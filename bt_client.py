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
from datetime import datetime
from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

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

# state variables to calculate water flow based on delta in total and time between updates
last_water_total: float = 0.0
last_water_time: float = 0.0

def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def rprint(*args, **kwargs) -> None:
    """Print with \\r\\n line endings so output is correct in raw terminal mode."""
    buf = io.StringIO()
    print(*args, file=buf, **kwargs)
    sys.stdout.write(buf.getvalue().replace("\n", "\r\n"))
    sys.stdout.flush()


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


async def read_key() -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: sys.stdin.read(1))


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

async def read_key() -> str | None:
    if sys.stdin in select.select([sys.stdin], [], [], 0.1)[0]:
        return sys.stdin.read(1)
    return None

async def keyboard_loop(client: BleakClient, stop_event: asyncio.Event) -> None:
    print_shortcuts()
    while not stop_event.is_set() and client.is_connected:
        key = await read_key()
        if key is not None:

            if key in ("\x03", "q"):
                rprint(f"\n{YELLOW}Quit requested.{RESET}")
                stop_event.set()
                break

            if key == "?":
                print_shortcuts()

            elif key in SHORTCUTS:
                payload, desc = SHORTCUTS[key]
                if payload is not None:
                    rprint(f"{CYAN}[{ts()}] → {desc}{RESET}")
                    await send_cmd(client, payload)

            else:
                rprint(f"{GREY}[{ts()}] Unknown key: {repr(key)}  (press ? for help){RESET}")
        
        else:
            await asyncio.sleep(1.0) # Yield to let the loop process the stop event

def make_disconnected_callback(stop_event: asyncio.Event):
    def callback(client: BleakClient):
        rprint(f"\n{RED}[{ts()}] Bluetooth connection lost.{RESET}")
        stop_event.set()

        # loop = asyncio.get_running_loop()
        # loop.stop()
    return callback

# ── Main connection logic ──────────────────────────────────────────────────────────

async def run(address: str) -> None:
    print(f"{YELLOW}Connecting to {address}...{RESET}")

    stop_event = asyncio.Event()
    disconnected_cb = make_disconnected_callback(stop_event=stop_event)

    async with BleakClient(address, timeout=20.0, disconnected_callback=disconnected_cb) as client:
        print(f"{GREEN}Connected to {address}{RESET}")

        services = [s for s in client.services if s.uuid.lower() == SERVICE_UUID.lower()]
        if not services:
            print(f"{RED}ESPHome service not found on device. Wrong device?{RESET}")
            return

        print(f"{YELLOW}Subscribing to log characteristic...{RESET}")
        await client.start_notify(LOG_CHAR_UUID, log_notification)

        print(f"{YELLOW}Subscribing to state characteristic...{RESET}")
        await client.start_notify(STATE_CHAR_UUID, state_notification)

        try:
            data = await client.read_gatt_char(STATE_CHAR_UUID)
            if data:
                print(f"{CYAN}[{ts()}] Last state (first 20 bytes): {data.decode('utf-8', errors='replace')}{RESET}")
        except Exception as e:
            print(f"{GREY}[{ts()}] Could not read state: {e}{RESET}")

        print(f"{GREEN}Ready. Keyboard shortcuts active (press ? for help):{RESET}")

        
        
        with RawTerminal():
            await keyboard_loop(client, stop_event)
        

        if not stop_event.is_set():
            # Restore normal terminal before printing disconnect messages
            print(f"\n{YELLOW}Disconnecting...{RESET}")        
            await client.stop_notify(LOG_CHAR_UUID)
            await client.stop_notify(STATE_CHAR_UUID)

    print(f"{GREEN}Disconnected.{RESET}")
    sys.exit(0)


async def main() -> None:
    parser = argparse.ArgumentParser(description="ESPHome BLE Logger")
    parser.add_argument("--name", "-n", help="Device name suffix", default=None)
    parser.add_argument("--address", "-a", help="BLE MAC address", default=None)
    args = parser.parse_args()

    try:
        address = await find_device(name_suffix=args.name, address=args.address)
        await run(address=address)
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Interrupted.{RESET}")


if __name__ == "__main__":
    asyncio.run(main())
