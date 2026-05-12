#!/usr/bin/env python3
"""
ESPHome BLE Logger
Connects to an ESPHome node over BLE and subscribes to log and sensor data characteristics.

UUIDs match the esp32_ble_server configuration in node.yaml:
  Service:     a1b2c3d4-e5f6-7890-abcd-ef1234567890
  Log char:    a1b2c3d4-e5f6-7890-abcd-ef1234567891  (notify, read)
  State char:  a1b2c3d4-e5f6-7890-abcd-ef1234567892  (notify, read)
  Cmd char:    a1b2c3d4-e5f6-7890-abcd-ef1234567894  (write)

Keyboard shortcuts (while connected):
  c        Send 'log_config' command to device
  q / ^C   Disconnect and quit
  ?        Show this help

Usage:
  python3 ble_logger.py                        # scan and connect to first node
  python3 ble_logger.py --name abc123          # connect by name suffix
  python3 ble_logger.py --address AA:BB:CC:..  # connect by MAC address

Install dependencies:
  pip install bleak
"""

import asyncio
import argparse
import io
import re
import select
import sys
import termios
import tty
import os
# import signal
from datetime import datetime
from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

import esp32_ble_ota_upload

# ── UUIDs ─────────────────────────────────────────────────────────────────────
SERVICE_UUID    = "43434353-0001-1000-8000-00805F9B34FB"
LOG_CHAR_UUID   = "43434353-0002-1000-8000-00805F9B34FB"
STATE_CHAR_UUID = "43434353-0003-1000-8000-00805F9B34FB"
CMD_CHAR_UUID   = "43434353-0004-1000-8000-00805F9B34FB"

# ── Keyboard shortcuts → (cmd_payload, description) ───────────────────────────
# payload=None  → handled specially in keyboard_loop
# payload=str   → written to CMD_CHAR_UUID automatically
# Add new commands here — key must be a single character.
SHORTCUTS: dict[str, tuple[str | None, str]] = {
    "c": ("log_config",     "Request log_config from device"),
    "p": ("ping",           "Send ping command to device"), 
    "s": ("update_sensors", "Request immediate sensor update from device"),
    "r": ("restart",        "Soft-Restart the device"),
    "?": (None,             "Show keyboard shortcuts"),
    "q": (None,             "Disconnect and quit"),
    "f": (None,             "OTA Firmware Upgrade")
}

# ── ANSI colours ──────────────────────────────────────────────────────────────
RESET  = "\033[0m"
GREY   = "\033[90m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
RED    = "\033[31m"
BOLD   = "\033[1m"
PURPLE = "\033[35m"
WHITE_ON_BLUE = "\033[44m"

# state variables to calculate water flow based on delta in total and time between updates
last_water_total: float = 0.0
last_water_time: float = 0.0

status_msg = None
status_input_mode = False
_status_timeout = None
_user_quit = False

_connected = False
_connected_name = None
_connected_address = None


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def rprint(*args, **kwargs) -> None:
    clear_status()
    """Print with \\r\\n line endings so output is correct in raw terminal mode."""
    buf = io.StringIO()
    print(*args, file=buf, **kwargs)
    # sys.stdout.write("\0337")
    sys.stdout.write(buf.getvalue().replace("\n", "\r\n"))
    # sys.stdout.write("\0338")
    sys.stdout.flush()
    render_status()


def print_shortcuts() -> None:
    rprint(f"\n{CYAN}{BOLD}Keyboard shortcuts:{RESET}")
    for key, (_, desc) in SHORTCUTS.items():
        rprint(f"  {BOLD}{key}{RESET}  {desc}")
    rprint()


# ── Raw terminal input ─────────────────────────────────────────────────────────

class RawTerminal:
    """Context manager: puts stdin into raw (single keypress) mode."""

    def __enter__(self):
        if sys.stdin.isatty():
            self._fd = sys.stdin.fileno()
            self._old = termios.tcgetattr(self._fd)
            tty.setraw(self._fd)
        return self

    def __exit__(self, *_):
        if sys.stdin.isatty():
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)


# async def read_key(stop_event) -> str:
#     loop = asyncio.get_event_loop()
#     read1 = lambda: sys.stdin.read(1)

#     key = await loop.run_in_executor(None, read1)

#     if key == "\x1b":
#         # check if more bytes are available (escape sequence vs bare ESC)
#         ready = select.select([sys.stdin], [], [], 0.05)[0]
#         if ready:
#             seq = sys.stdin.read(1)  # safe to read synchronously, we know data is there
#             if seq == "[":
#                 sys.stdin.read(1)  # consume the code byte
#             return None  # discard all escape sequences
#         else:
#             return "\x1b"  # bare ESC, nothing followed

#     return key
_read_cancelled = False

def _blocking_read() -> str | None:
    global _read_cancelled
    while True:
        ready = select.select([sys.stdin], [], [], 0.05)[0]
        if ready:
            return sys.stdin.read(1)
        if _read_cancelled:
            _read_cancelled = False
            return None

async def read_key(stop_event: asyncio.Event) -> str | None:
    loop = asyncio.get_event_loop()

    key_future = loop.run_in_executor(None, _blocking_read)
    stop_future = asyncio.ensure_future(stop_event.wait())

    done, pending = await asyncio.wait(
        [key_future, stop_future],
        return_when=asyncio.FIRST_COMPLETED
    )

    for task in pending:
        task.cancel()

    if stop_event.is_set():
        global _read_cancelled
        _read_cancelled = True
        return None

    raw_key = key_future.result()

    if raw_key == "\x1b":
        ready = select.select([sys.stdin], [], [], 0.05)[0]
        if ready:
            seq = sys.stdin.read(1)
            if seq == "[":
                sys.stdin.read(1)
            return None
        else:
            return "\x1b"

    return raw_key


# ── Notification callbacks ─────────────────────────────────────────────────────

def log_notification(characteristic: BleakGATTCharacteristic, data: bytearray) -> None:
    try:
        msg = data.decode("utf-8", errors="replace").strip()
        # regex match for format [I][log_config:593]:
        if msg.startswith("[C]"):

            prefix_end = msg.find("]: ")
            
            if prefix_end != -1:
                prefix = msg[:prefix_end + 3]
                rest = msg[prefix_end + 3:]
                lines = rest.split("\n")
                for line in lines:
                    if line.strip():
                        rprint(f"{GREY}[{ts()}]{RESET} {PURPLE}{prefix}{line}{RESET}")

            else:
               rprint(f"{GREY}[{ts()}]{RESET} {PURPLE}{msg}{RESET}")

        elif msg.startswith("[I]"):
            rprint(f"{GREY}[{ts()}]{RESET} {GREEN}{msg}{RESET}")
        elif msg.startswith("[D]"):
            rprint(f"{GREY}[{ts()}]{RESET} {CYAN}{msg}{RESET}")
        elif msg.startswith("[W]"):
            rprint(f"{GREY}[{ts()}]{RESET} {YELLOW}{msg}{RESET}")
        elif msg.startswith("[E]"):
            rprint(f"{GREY}[{ts()}]{RESET} {RED}{msg}{RESET}")
        else:
            rprint(f"{GREY}[{ts()}]{RESET} {msg}")
    except Exception as e:
        rprint(f"{RED}[{ts()}] Error decoding log: {e}{RESET}")


def state_notification(characteristic: BleakGATTCharacteristic, data: bytearray) -> None:
    try:
        msg = data.decode("utf-8", errors="replace").strip()
        parts = msg.split("|")
        rprint(f"\n{CYAN}{'─' * 60}{RESET}")
        rprint(f"{GREEN}[{ts()}] SENSOR UPDATE{RESET}")
        if len(parts) >= 8:
            rprint(f"  Device      : {parts[1]}")
            rprint(f"  Uptime      : {parts[2]}s")
            rprint(f"  MCU Temp    : {parts[3]}°C")
            rprint(f"  Ext Temp    : {parts[4]}°C")
            rprint(f"  Current A   : {parts[5]}A")
            rprint(f"  Current B   : {parts[6]}A")
            rprint(f"  Water Used  : {parts[7]}L")


            # calculate water flow based on change in total and time since last update
            global last_water_total
            global last_water_time
                        
            water_total = float(parts[7])
            
            water_diff = water_total - last_water_total if water_total >= last_water_total else water_total # handle counter reset
            read_time = float(parts[2])
            time_diff = read_time - last_water_time if read_time >= last_water_time else read_time # handle uptime reset
            water_flow = (water_diff / time_diff * 60) if time_diff > 0 else 0.0
            
            last_water_total = water_total
            last_water_time = read_time
            rprint(f"  Water Flow  : {water_flow:.2f}L/m (calculated)")


        else:
            rprint(f"  Raw: {msg}")
        rprint(f"{CYAN}{'─' * 60}{RESET}\n")
    except Exception as e:
        rprint(f"{RED}[{ts()}] Error decoding state: {e}{RESET}")


# ── Device discovery ───────────────────────────────────────────────────────────

async def find_device(name_suffix: str = None, address: str = None) -> str:
    if address:
        return address

    print(f"{YELLOW}Scanning for ESPHome nodes...{RESET}")
    devices = await BleakScanner.discover(timeout=2.0)

    if name_suffix:
        devices = [d for d in devices if d.name and d.name.endswith(name_suffix.lower())]
    else:
        devices = [d for d in devices if d.name and "node-" in d.name.lower()]

    if not devices:
        print(f"{YELLOW}No devices found with initial filter, scanning more broadly...{RESET}")
        devices = await BleakScanner.discover(timeout=10.0)
        if name_suffix:
            devices = [d for d in devices if d.name and d.name.lower().endswith(name_suffix.lower())]
        else:
            devices = [d for d in devices if d.name and d.name.lower().startswith("node-")]

    if not devices:
        print(f"{RED}No ESPHome nodes found.{RESET}")
        sys.exit(1)

    if len(devices) == 1:
        print(f"{GREEN}Found: {devices[0].name} ({devices[0].address}){RESET}")
        return devices[0].address

    # Multiple devices found — present menu
    print(f"{YELLOW}Multiple devices found:{RESET}\n")
    for i, d in enumerate(devices, 1):
        print(f"  {BOLD}{i}{RESET}  {d.name:<20}  {d.address}")

    while True:
        try:
            choice = input(f"\n{CYAN}Select device (1-{len(devices)}): {RESET}")
            idx = int(choice) - 1
            if 0 <= idx < len(devices):
                selected = devices[idx]
                print(f"{GREEN}Selected: {selected.name} ({selected.address}){RESET}\n")
                return selected.address
            else:
                print(f"{RED}Invalid selection. Please enter a number between 1 and {len(devices)}.{RESET}")
        except ValueError:
            print(f"{RED}Invalid input. Please enter a number.{RESET}")

# ── Commands ───────────────────────────────────────────────────────────────────

async def send_cmd(client: BleakClient, payload: str) -> None:
    try:
        await client.write_gatt_char(CMD_CHAR_UUID, payload.encode("utf-8"), response=True)
        rprint(f"{GREEN}[{ts()}] CMD sent: {payload}{RESET}")
    except Exception as e:
        rprint(f"{RED}[{ts()}] Failed to send cmd '{payload}': {e}{RESET}")


# ── Keyboard loop ──────────────────────────────────────────────────────────────

async def keyboard_loop(client: BleakClient, stop_event: asyncio.Event, raw_term: RawTerminal) -> None:
    print_shortcuts()

    entry_mode = False
    entry_prefix = ""
    entry_buffer = ""
    entry_action = None
    ota_firmware_path = None

    while not stop_event.is_set() and client.is_connected:
        key = await read_key(stop_event)
        if key is not None:

            if entry_mode:
                # if not the enter key
                if key in ("\r", "\n"):

                    if entry_action == "ota_firmware":
                        ota_firmware_path = entry_buffer
                        entry_mode = False
                        entry_buffer = ""
                        entry_prefix = ""

                        if os.path.exists(ota_firmware_path):
                            # prompt for password next
                            entry_prefix = "OTA Upload -- Password: "
                            entry_buffer = ""
                            entry_mode = True
                            entry_action = "ota_password"
                            set_status(entry_prefix, input_mode=True)
                        else:
                            entry_mode = False
                            entry_buffer = ""
                            entry_prefix = ""
                            set_status("  OTA Failed: File Not Found", timeout=5.0)

                    elif entry_action == "ota_password":
                        ota_firmware_password = entry_buffer
                        entry_mode = False
                        entry_buffer = ""
                        entry_prefix = ""                
    
                        asyncio.ensure_future(ota_upload(client, ota_firmware_path, ota_firmware_password, stop_event))
                elif key in ("\x7f"):
                    entry_buffer = entry_buffer[0:-1]
                    set_status(f"{entry_prefix}{entry_buffer}", input_mode=True)
                elif key in ("\x1b"):
                    entry_buffer = ""
                    entry_prefix = ""
                    entry_mode = False
                    set_status(None)
                elif key.isprintable():
                    entry_buffer += key
                    set_status(f"{entry_prefix}{entry_buffer}", input_mode=True)

            elif key in ("\x03", "q"):
                rprint(f"\n{YELLOW}Quit requested.{RESET}")
                global _user_quit
                _user_quit = True
                stop_event.set()
                break

            elif key == "?":
                print_shortcuts()

            elif key == "f":
                # keyboard entry loop state machine
                entry_prefix = "OTA Upload -- firmware.ota.bin path: "
                entry_buffer = ""
                entry_mode = True
                entry_action = "ota_firmware"
                set_status(entry_prefix, input_mode=True)
                


            elif key in SHORTCUTS:
                payload, desc = SHORTCUTS[key]
                if payload is not None:
                    rprint(f"{CYAN}[{ts()}] → {desc}{RESET}")
                    await send_cmd(client, payload)

            elif key in ("\r", "\n"):
                rprint("")

            else:
                rprint(f"{GREY}[{ts()}] Unknown key: {repr(key)}  (press ? for help){RESET}")
        
        #else:
            # why does this block writing to the terminal output? 
            #await asyncio.sleep(0.1) # Yield to let the loop process the stop event

def make_disconnected_callback(stop_event: asyncio.Event):
    def callback(client: BleakClient):
        rprint(f"\n{RED}[{ts()}] Bluetooth connection lost.{RESET}")        
        stop_event.set()

        # loop = asyncio.get_running_loop()
        # loop.stop()
    return callback

# def _on_resize(signum, frame):
#     clear_status()
#     render_status()

# signal.signal(signal.SIGWINCH, _on_resize)

def set_status(message: str, input_mode=False, timeout=None):
    global status_msg, status_input_mode, _status_timeout
    status_msg = message
    status_input_mode = input_mode
    render_status()

    # cancel any existing timeouts, and schedule a new one if one is provided
    if _status_timeout is not None:
        _status_timeout.cancel()
        _status_timeout = None

    if timeout is not None:
        _status_timeout = asyncio.get_event_loop().call_later(timeout, lambda: set_status(None))

# strip ANSI codes before measuring for padding
# def strip_ansi(s: str) -> str:
#     return re.sub(r'\033\[[0-9;]*[A-Za-z]', '', s)

# def set_scroll_region(rows: int) -> None:
#     # reserve the bottom line for the status bar
#     sys.stdout.write(f"\033[1;{rows-1}r")  # scroll region: row 1 to rows-1
#     sys.stdout.write(f"\033[{rows-1};1H")  # move cursor to last scroll row
#     sys.stdout.flush()

# def clear_scroll_region() -> None:
#     rows = os.get_terminal_size().lines
#     sys.stdout.write(f"\033[1;{rows}r")    # restore full scroll region
#     sys.stdout.write(f"\033[{rows};1H")
#     sys.stdout.flush()

def render_status() -> None:
    # rows, cols = shutil.get_terminal_size()
    tsize = os.get_terminal_size()
    cols = tsize.columns

    msg = status_msg
    if msg is None:
        if _connected:
            prefix = f"  Connected > {_connected_name} [{_connected_address}]"
        else:
            prefix = f"  Disconnected."
        suffix = "[?] help "
        center_padding = cols - len(prefix) - len(suffix)
        msg = f"{prefix}{" "*center_padding}{suffix}"

    if len(msg) > cols:
        msg = msg[-1 * cols:]

    # Clear the line first to avoid leftover chars if msg shrinks
    #visible_len = len(strip_ansi(msg))
    #padded = msg + " " * max(0, cols - 1 - visible_len)
    padded = msg + " " * max(0, cols - 1 - len(msg))
    # \0337 = save cursor, \033[{row};0H = move to bottom row, \0338 = restore cursor
    #sys.stdout.write(f"\0337\033[{rows};0H\033[2K{WHITE_ON_BLUE}{padded}{RESET}\0338")
    cursor_col = 1
    if ( status_input_mode ):
        cursor_col = len(msg)

    # sys.stdout.write(f"\033[{rows};0H\033[2K{WHITE_ON_BLUE}{padded}{RESET}\033[{rows};{cursor_col}H")
    sys.stdout.write(f"\r\033[2K{WHITE_ON_BLUE}{padded}{RESET}\r") #\033[{cursor_col}C
    if ( status_input_mode ):
        sys.stdout.write(f"\033[{cursor_col}C")

    sys.stdout.flush()

def clear_status() -> None:
    columns = os.get_terminal_size().columns
    # sys.stdout.write(f"\0337\033[{rows};0H\033[2K\0338")
    #sys.stdout.write(f"\033[{rows};0H\033[2K\033[{rows};1H")
    sys.stdout.write(f"\r{" "*columns}\r")
    sys.stdout.flush()

async def ota_upload(client, path, password, stop_event):
    try:
        with open(path, 'rb') as f:
            firmware = f.read()

        uploader = esp32_ble_ota_upload.ESP32BLEOTAUploader(firmware=firmware, password=password)
        await uploader.upload(client, on_progress=ota_upload_on_progress)
        set_status(f" OTA Upload Complete")

    except FileNotFoundError:
        set_status("  OTA Failed: File Not Found", timeout=5.0)
    except ValueError as e:
        rprint(f"{RED}[OTA] OTA Failed: {e}{RESET}")
        set_status(f"  OTA Failed: {e}", timeout=5.0)

    # don't catch other exceptions, let it blow up the script to log a stacktrace

def ota_upload_on_progress(pct, rate, elapsed, eta):
    bar     = "█" * (pct // 5) + "░" * (20 - pct // 5)
    set_status(f"  OTA [{bar}] {pct:3d}%  {rate:4.1f} KB/s  ETA {int(eta)}s")
    

    

# ── Main connection logic ──────────────────────────────────────────────────────────

async def run(args) -> None:

    global _connected, _connected_name, _connected_address
    
    address = await find_device(name_suffix=args.name, address=args.address)
    
    while True:
        print(f"{YELLOW}Connecting to {address}...{RESET}")

        stop_event = asyncio.Event()
        disconnected_cb = make_disconnected_callback(stop_event=stop_event)

        try:
            async with BleakClient(address, timeout=20.0, disconnected_callback=disconnected_cb) as client:
                print(f"{GREEN}Connected to {address}{RESET}")
                # set_status(f"  Connected: {client.name} [{address}]")

                _connected = True
                _connected_name = client.name
                _connected_address = client.address

                services = [s for s in client.services if s.uuid.lower() == SERVICE_UUID.lower()]
                if not services:
                    print(f"{RED}ESPHome service not found on device. Wrong device?{RESET}")
                    return

                print(f"{YELLOW}Subscribing to log characteristic...{RESET}")
                await client.start_notify(LOG_CHAR_UUID, log_notification)

                print(f"{YELLOW}Subscribing to state characteristic...{RESET}")
                await client.start_notify(STATE_CHAR_UUID, state_notification)

                print(f"{GREEN}Ready. Keyboard shortcuts active (press ? for help):{RESET}")

                with RawTerminal() as raw_term:
                    # set_scroll_region(os.get_terminal_size().lines)
                    await keyboard_loop(client, stop_event, raw_term)
                    # clear_scroll_region()
                await asyncio.sleep(0.1)
                clear_status()
                    
                if not stop_event.is_set() or _user_quit:
                    # Restore normal terminal before printing disconnect messages
                    
                    print(f"\n{YELLOW}Disconnecting...{RESET}")        
                    
                    await client.stop_notify(LOG_CHAR_UUID)
                    await client.stop_notify(STATE_CHAR_UUID)
                    _connected = False
                    _connected_name = None
                    _connected_address = None
                    break

        except Exception as e:
            print(f"{RED}Connection error: {e}{RESET}")
            

        _connected = False
        _connected_name = None
        _connected_address = None
        print(f"{YELLOW}Reconnecting in 3 seconds...{RESET}")
        await asyncio.sleep(3)
    
            

    clear_status()
    print(f"\r\n{GREEN}Disconnected.{RESET}\n")
    # clear_status()
    sys.exit(0)


async def main() -> None:
    parser = argparse.ArgumentParser(description="ESPHome BLE Logger")
    parser.add_argument("--name", "-n", help="Device name suffix", default=None)
    parser.add_argument("--address", "-a", help="BLE MAC address", default=None)
    args = parser.parse_args()

    try:
        await run(args)
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Interrupted.{RESET}")


if __name__ == "__main__":
    asyncio.run(main())
