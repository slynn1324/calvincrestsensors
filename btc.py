#!/usr/bin/env python3
"""
ESPHome BLE Logger
Connects to an ESPHome node over BLE and subscribes to log and sensor data characteristics.

UUIDs match the esp32_ble_server configuration in node.yaml:
  Service:     43434353-0001-1000-8000-00805F9B34FB
  Log char:    43434353-0002-1000-8000-00805F9B34FB  (notify, read)
  State char:  43434353-0003-1000-8000-00805F9B34FB  (notify, read)
  Cmd char:    43434353-0004-1000-8000-00805F9B34FB  (write)

Keyboard shortcuts (while connected):
  c        Send 'log_config' command to device
  p        Send 'ping' command to device
  s        Request immediate sensor update
  r        Soft-restart the device
  f        OTA firmware upgrade (prompts for path and password)
  q / ^C   Disconnect and quit
  ?        Show this help

Usage:
  python3 ble_logger.py                        # scan and connect to first matching node
  python3 ble_logger.py --name abc123          # connect by name suffix
  python3 ble_logger.py --address AA:BB:CC:..  # connect by MAC address

Dependencies:
  pip install bleak
"""

import asyncio
import argparse
import io
import os
import select
import sys
import termios
import tty
from datetime import datetime

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

import esp32_ble_ota_upload


# ── UUIDs ──────────────────────────────────────────────────────────────────────

SERVICE_UUID    = "43434353-0001-1000-8000-00805F9B34FB"
LOG_CHAR_UUID   = "43434353-0002-1000-8000-00805F9B34FB"
STATE_CHAR_UUID = "43434353-0003-1000-8000-00805F9B34FB"
CMD_CHAR_UUID   = "43434353-0004-1000-8000-00805F9B34FB"


# ── ANSI colour codes ──────────────────────────────────────────────────────────

RESET         = "\033[0m"
GREY          = "\033[90m"
GREEN         = "\033[32m"
YELLOW        = "\033[33m"
CYAN          = "\033[36m"
RED           = "\033[31m"
BOLD          = "\033[1m"
PURPLE        = "\033[35m"
WHITE_ON_BLUE = "\033[44m"


# ── Keyboard shortcut registry ─────────────────────────────────────────────────
# Each entry: key → (cmd_payload | None, description)
#   payload=None  → handled specially in the keyboard loop
#   payload=str   → written to CMD_CHAR_UUID automatically

SHORTCUTS: dict[str, tuple[str | None, str]] = {
    "c": ("log_config",     "Request log_config from device"),
    "p": ("ping",           "Send ping command to device"),
    "s": ("update_sensors", "Request immediate sensor update from device"),
    "r": ("restart",        "Soft-restart the device"),
    "f": (None,             "OTA firmware upgrade"),
    "?": (None,             "Show keyboard shortcuts"),
    "q": (None,             "Disconnect and quit"),
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def ts() -> str:
    """Return current time as HH:MM:SS.mmm string."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


# ── Raw terminal context manager ───────────────────────────────────────────────

class RawTerminal:
    """
    Context manager that puts stdin into raw (single-keypress) mode while active,
    then restores the previous terminal settings on exit.

    Only acts when stdin is a real TTY; safe to use in piped contexts.
    """
    def __init__(self):
        self._read_cancelled = False # Set to True by the async reader when it wants the blocking thread to abort.

    def __enter__(self) -> "RawTerminal":
        self._fd: int | None = None
        self._saved_attrs = None
        if sys.stdin.isatty():
            self._fd = sys.stdin.fileno()
            self._saved_attrs = termios.tcgetattr(self._fd)
            tty.setraw(self._fd)
        return self

    def __exit__(self, *_) -> None:
        if self._fd is not None and self._saved_attrs is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved_attrs)


    # ── Output Writing ─────────────────────────────────────────────────────────────

    def rprint(self, *args, **kwargs) -> None:
        """
        Print with \\r\\n line endings (required in raw-terminal mode), then
        redraw the status bar below the new output.
        """
        buf = io.StringIO()
        print(*args, file=buf, **kwargs)
        sys.stdout.write(buf.getvalue().replace("\n", "\r\n"))
        sys.stdout.flush()

    # ── Key reading ────────────────────────────────────────────────────────────────

    def _blocking_read_stdin(self) -> str | None:
        """
        Block in a polling loop (50 ms ticks) until a character arrives on stdin,
        returning it — or None if _read_cancelled is set externally.
        """ 
        while True:
            ready = select.select([sys.stdin], [], [], 0.05)[0]
            if ready:
                return sys.stdin.read(1)
            if self._read_cancelled:
                self._read_cancelled = False
                return None


    async def read_key(self, stop_event: asyncio.Event) -> str | None:
        """
        Await a single keypress without blocking the event loop.

        Returns None when stop_event fires, or when an ANSI escape sequence is
        received (sequences are consumed but ignored).
        """
        loop = asyncio.get_event_loop()

        key_future  = loop.run_in_executor(None, self._blocking_read_stdin)
        stop_future = asyncio.ensure_future(stop_event.wait())

        done, pending = await asyncio.wait(
            [key_future, stop_future],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()

        if stop_event.is_set():
            self._read_cancelled = True   # signal the blocking thread to exit its loop
            return None

        raw_key = key_future.result()

        # Discard ANSI escape sequences (arrow keys, function keys, etc.) to avoid
        # polluting the keyboard handler with multi-byte garbage.
        if raw_key == "\x1b":
            ready = select.select([sys.stdin], [], [], 0.05)[0]
            if ready:
                seq = sys.stdin.read(1)
                if seq == "[":
                    sys.stdin.read(1)   # consume the final byte of CSI sequences
                return None             # discard the whole sequence
            return "\x1b"               # bare ESC with nothing following

        return raw_key



# ── Status bar ─────────────────────────────────────────────────────────────────

class RawTerminalWithStatusBar(RawTerminal):
    """
    Manages a persistent status bar rendered on the current terminal line using
    in-place ANSI overwrites (no alternate screen or scroll-region required).

    Inherits RawTerminal so callers can use a single `with StatusBar(...) as sb:`
    block to activate both raw-mode input and the status bar together.

    Usage:
        with StatusBar(connected_name="node-abc", connected_address="AA:BB...") as sb:
            sb.render()                        # draw the default connected status
            sb.set("Uploading…", timeout=5.0)  # show a timed message
            sb.set("Enter path: /fw", input_mode=True)  # input prompt (cursor shown)
            sb.clear()                         # blank the line before printing
    """

    def __init__(self, connected_name: str = None, connected_address: str = None) -> None:
        super().__init__()
        self.connected_name    = connected_name
        self.connected_address = connected_address

        self._message: str | None = None       # override text; None = default status
        self._input_mode: bool    = False       # whether to show a visible cursor
        self._timeout_handle      = None        # asyncio TimerHandle for auto-clear

    def __exit__(self, *_) -> None:
        self.clear()
        super().__exit__(*_)
        self.clear()

    # ── Public API ─────────────────────────────────────────────────────────────

    def set_status(self, message: str | None, *, input_mode: bool = False, timeout: float | None = None) -> None:
        """
        Set the status bar text.

        Args:
            message:    Text to display, or None to revert to the default
                        "Connected / Disconnected" status line.
            input_mode: When True the cursor is positioned at the end of the
                        message so the user can see what they are typing.
            timeout:    If given, automatically clear back to the default status
                        after this many seconds.
        """
        self._message    = message
        self._input_mode = input_mode
        self.render()

        # Cancel any pending auto-clear before scheduling a new one.
        if self._timeout_handle is not None:
            self._timeout_handle.cancel()
            self._timeout_handle = None

        if timeout is not None:
            self._timeout_handle = asyncio.get_event_loop().call_later(
                timeout, lambda: self.set(None)
            )

    def clear(self) -> None:
        """Erase the status bar line so normal output can be printed above it."""
        cols = os.get_terminal_size().columns
        sys.stdout.write(f"\r{' ' * cols}\r")
        sys.stdout.flush()

    def render(self) -> None:
        """Redraw the status bar in place on the current line."""
        cols = os.get_terminal_size().columns
        msg  = self._build_message(cols)

        # Pad to fill the terminal width (leave one cell spare to avoid wrapping).
        padded = msg + " " * max(0, cols - 1 - len(msg))

        sys.stdout.write(f"\r\033[2K{WHITE_ON_BLUE}{padded}{RESET}\r")

        if self._input_mode:
            # Advance the cursor to the end of the visible message so the user
            # can see the insertion point while typing.
            sys.stdout.write(f"\033[{len(msg)}C")

        sys.stdout.flush()

    def rprint(self, *args, **kwargs) -> None:
        """
        Clear and redraw the status bar around RawTerminal's rprint
        """
        self.clear()
        super().rprint(*args, **kwargs)
        self.render()

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _build_message(self, cols: int) -> str:
        """Build the status bar string, truncating if it exceeds the terminal width."""
        if self._message is not None:
            msg = self._message
        elif self.connected_name:
            prefix = f"  Connected > {self.connected_name} [{self.connected_address}]"
            suffix = "[?] help "
            padding = max(0, cols - len(prefix) - len(suffix))
            msg = f"{prefix}{' ' * padding}{suffix}"
        else:
            msg = "  Disconnected.   [?] help "

        # Truncate to terminal width from the left to preserve the tail (most
        # relevant when a long path is being typed into an input prompt).
        if len(msg) > cols:
            msg = msg[-cols:]

        return msg






# ── BLE client wrapper ─────────────────────────────────────────────────────────

class BLELoggerClient:
    """
    Wraps a BleakClient and owns all application-level state for one BLE session:
      - notification callbacks (log and sensor data)
      - command dispatch
      - OTA firmware upload
      - keyboard interaction loop
      - status bar lifecycle

    Instantiate once per connection attempt; do not reuse across reconnects.
    """

    def __init__(self, address: str) -> None:
        self.address       = address
        self._client: BleakClient | None = None
        self._term: RawTerminalWithStatusBar   | None = None
        self._user_quit: bool            = False

        # Water-flow state: tracks the previous sensor reading to calculate
        # a rate-of-change between consecutive updates.
        self._last_water_total: float = 0.0
        self._last_water_time:  float = 0.0

    # ── Connection lifecycle ───────────────────────────────────────────────────

    async def connect_and_run(self) -> bool:
        """
        Connect, subscribe, run the interactive loop, and clean up.

        Returns True  if the user requested an explicit quit (no reconnect).
                False if the connection dropped unexpectedly (caller should retry).
        """
        stop_event      = asyncio.Event()
        disconnected_cb = self._make_disconnected_callback(stop_event)

        async with BleakClient(
            self.address,
            timeout=20.0,
            disconnected_callback=disconnected_cb,
        ) as client:
            self._client = client
            print(f"{GREEN}Connected to {self.address}{RESET}")

            if not self._verify_service():
                print(f"{RED}ESPHome service not found on device. Wrong device?{RESET}")
                return True   # don't retry — this isn't our device

            print(f"{YELLOW}Subscribing to log characteristic...{RESET}")
            await client.start_notify(LOG_CHAR_UUID, self._on_log_notification)

            print(f"{YELLOW}Subscribing to state characteristic...{RESET}")
            await client.start_notify(STATE_CHAR_UUID, self._on_state_notification)

            print(f"{GREEN}Ready. Press ? for keyboard shortcuts.{RESET}")

            with RawTerminalWithStatusBar(connected_name=client.name, connected_address=client.address) as term:
                self._term = term
                await self._keyboard_loop(stop_event)

            await asyncio.sleep(0.1)   # let any in-flight callbacks drain

            if self._user_quit:
                print(f"\n{YELLOW}Disconnecting...{RESET}")
                await client.stop_notify(LOG_CHAR_UUID)
                await client.stop_notify(STATE_CHAR_UUID)
                return True   # caller should not reconnect

        # Reaching here means the `async with` block exited, either cleanly
        # or because the disconnection callback fired.
        self._client = None
        return self._user_quit

    def _verify_service(self) -> bool:
        """Return True if the expected ESPHome BLE service is present."""
        return any(
            s.uuid.lower() == SERVICE_UUID.lower()
            for s in self._client.services
        )

    def _make_disconnected_callback(self, stop_event: asyncio.Event):
        """Return a BleakClient disconnection callback that signals stop_event."""
        def _callback(client: BleakClient) -> None:
            self._rprint(f"\n{RED}[{ts()}] Bluetooth connection lost.{RESET}")
            self._term.clear() # not sure why I need to clear here again... but it seems that I do
            stop_event.set()
        return _callback

    # ── Output helpers ─────────────────────────────────────────────────────────

    def _rprint(self, *args, **kwargs) -> None:
        """Route print calls through the status bar so the bar is preserved."""
        if self._term:
            self._term.rprint(*args, **kwargs)
        else:
            # Fallback before the status bar is initialized (e.g. in callbacks
            # that fire very early or very late in the connection lifecycle).
            buf = io.StringIO()
            print(*args, file=buf, **kwargs)
            sys.stdout.write(buf.getvalue().replace("\n", "\r\n"))
            sys.stdout.flush()

    # ── Notification callbacks ─────────────────────────────────────────────────

    def _on_log_notification(
        self, characteristic: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle incoming log characteristic notifications and colour them by level."""
        try:
            msg = data.decode("utf-8", errors="replace").strip()

            # Config messages ([C]) can contain embedded newlines — split and
            # print each non-empty line separately so alignment is preserved.
            if msg.startswith("[C]"):
                prefix_end = msg.find("]: ")
                if prefix_end != -1:
                    prefix = msg[: prefix_end + 3]
                    for line in msg[prefix_end + 3 :].split("\n"):
                        if line.strip():
                            self._rprint(f"{GREY}[{ts()}]{RESET} {PURPLE}{prefix}{line}{RESET}")
                else:
                    self._rprint(f"{GREY}[{ts()}]{RESET} {PURPLE}{msg}{RESET}")

            elif msg.startswith("[I]"):
                self._rprint(f"{GREY}[{ts()}]{RESET} {GREEN}{msg}{RESET}")
            elif msg.startswith("[D]"):
                self._rprint(f"{GREY}[{ts()}]{RESET} {CYAN}{msg}{RESET}")
            elif msg.startswith("[W]"):
                self._rprint(f"{GREY}[{ts()}]{RESET} {YELLOW}{msg}{RESET}")
            elif msg.startswith("[E]"):
                self._rprint(f"{GREY}[{ts()}]{RESET} {RED}{msg}{RESET}")
            else:
                self._rprint(f"{GREY}[{ts()}]{RESET} {msg}")

        except Exception as e:
            self._rprint(f"{RED}[{ts()}] Error decoding log: {e}{RESET}")

    def _on_state_notification(
        self, characteristic: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Parse pipe-delimited sensor state packets and display a formatted block."""
        try:
            msg   = data.decode("utf-8", errors="replace").strip()
            parts = msg.split("|")

            self._rprint(f"\n{CYAN}{'─' * 60}{RESET}")
            self._rprint(f"{GREEN}[{ts()}] SENSOR UPDATE{RESET}")

            if len(parts) >= 8:
                # Expected format: <ignored>|device|uptime_s|mcu_temp|ext_temp|curr_a|curr_b|water_total
                self._rprint(f"  Device      : {parts[1]}")
                self._rprint(f"  Uptime      : {parts[2]}s")
                self._rprint(f"  MCU Temp    : {parts[3]}°C")
                self._rprint(f"  Ext Temp    : {parts[4]}°C")
                self._rprint(f"  Current A   : {parts[5]}A")
                self._rprint(f"  Current B   : {parts[6]}A")
                self._rprint(f"  Water Used  : {parts[7]}L")
                self._rprint(f"  Water Flow  : {self._calc_water_flow(parts[7], parts[2]):.2f} L/min (calculated)")
            else:
                self._rprint(f"  Raw: {msg}")

            self._rprint(f"{CYAN}{'─' * 60}{RESET}\n")

        except Exception as e:
            self._rprint(f"{RED}[{ts()}] Error decoding state: {e}{RESET}")

    def _calc_water_flow(self, raw_total: str, raw_uptime: str) -> float:
        """
        Calculate water flow rate (L/min) from consecutive sensor packets.

        Handles both counter and uptime resets by treating the new reading as
        an absolute delta rather than a difference from the previous value.
        """
        water_total = float(raw_total)
        uptime      = float(raw_uptime)

        # If either value decreased, the counter/uptime rolled over — treat the
        # new reading itself as the delta so we don't report negative flow.
        water_delta = (
            water_total - self._last_water_total
            if water_total >= self._last_water_total
            else water_total
        )
        time_delta = (
            uptime - self._last_water_time
            if uptime >= self._last_water_time
            else uptime
        )

        self._last_water_total = water_total
        self._last_water_time  = uptime

        return (water_delta / time_delta * 60) if time_delta > 0 else 0.0

    # ── Command dispatch ───────────────────────────────────────────────────────

    async def _send_cmd(self, payload: str) -> None:
        """Write a UTF-8 command string to the command characteristic."""
        try:
            await self._client.write_gatt_char(
                CMD_CHAR_UUID, payload.encode("utf-8"), response=True
            )
            self._rprint(f"{GREEN}[{ts()}] CMD sent: {payload}{RESET}")
        except Exception as e:
            self._rprint(f"{RED}[{ts()}] Failed to send cmd '{payload}': {e}{RESET}")

    # ── OTA firmware upload ────────────────────────────────────────────────────

    async def _run_ota_upload(self, path: str, password: str) -> None:
        """Read firmware from disk and stream it to the device via BLE OTA."""
        try:
            with open(path, "rb") as f:
                firmware = f.read()

            uploader = esp32_ble_ota_upload.ESP32BLEOTAUploader(
                firmware=firmware, password=password
            )
            await uploader.upload(self._client, on_progress=self._on_ota_progress)
            self._term.set_status(" OTA Upload Complete")

        except FileNotFoundError:
            self._term.set_status("  OTA Failed: File Not Found", timeout=5.0)
        except ValueError as e:
            self._rprint(f"{RED}[OTA] OTA Failed: {e}{RESET}")
            self._term.set_status(f"  OTA Failed: {e}", timeout=5.0)
        # Other exceptions intentionally propagate so the full traceback is logged.

    def _on_ota_progress(self, pct: int, rate: float, elapsed: float, eta: float) -> None:
        """Progress callback for OTA upload — updates the status bar."""
        filled  = "█" * (pct // 5)
        unfilled = "░" * (20 - pct // 5)
        self._term.set_status(f"  OTA [{filled}{unfilled}] {pct:3d}%  {rate:4.1f} KB/s  ETA {int(eta)}s")

    # ── Keyboard interaction loop ──────────────────────────────────────────────

    async def _keyboard_loop(self, stop_event: asyncio.Event) -> None:
        """
        Main interactive loop.  Reads single keypresses and dispatches them
        to command handlers.  Also manages a lightweight state machine for
        multi-character input prompts (OTA file path, OTA password).
        """
        self._print_shortcuts()

        # ── Text-entry state machine ───────────────────────────────────────────
        # When entry_mode is True, keystrokes are accumulated into entry_buffer
        # rather than being interpreted as shortcuts.
        entry_mode:   bool        = False
        entry_prefix: str         = ""
        entry_buffer: str         = ""
        entry_action: str | None  = None
        ota_path:     str | None  = None

        while not stop_event.is_set() and self._client.is_connected:
            key = await self._term.read_key(stop_event)
            if key is None:
                continue

            if entry_mode:
                if key in ("\r", "\n"):
                    entry_mode, entry_buffer, entry_prefix, ota_path = await self._handle_entry_submit(
                        entry_action, entry_buffer, ota_path, stop_event,
                    )
                    if entry_mode:
                        # _handle_entry_submit transitions to the next prompt;
                        # update entry_prefix and entry_action from its return.
                        # (Handled inline by re-reading the updated state below.)
                        entry_action = "ota_password"
                        entry_prefix = "OTA Upload -- Password: "
                        self._term.set_status(entry_prefix, input_mode=True)

                elif key == "\x7f":              # backspace
                    entry_buffer = entry_buffer[:-1]
                    self._term.set_status(f"{entry_prefix}{entry_buffer}", input_mode=True)

                elif key == "\x1b":              # ESC — cancel input
                    entry_mode, entry_buffer, entry_prefix, entry_action = False, "", "", None
                    self._term.set_status(None)

                elif key.isprintable():
                    entry_buffer += key
                    self._term.set_status(f"{entry_prefix}{entry_buffer}", input_mode=True)

            elif key in ("\x03", "q"):           # Ctrl-C or q
                self._rprint(f"\n{YELLOW}Quit requested.{RESET}")
                self._user_quit = True
                stop_event.set()
                break

            elif key == "?":
                self._print_shortcuts()

            elif key == "f":
                entry_prefix = "OTA Upload -- firmware path (.ota.bin): "
                entry_buffer = ""
                entry_mode   = True
                entry_action = "ota_firmware"
                self._term.set_status(entry_prefix, input_mode=True)

            elif key in SHORTCUTS:
                payload, desc = SHORTCUTS[key]
                if payload is not None:
                    self._rprint(f"{CYAN}[{ts()}] → {desc}{RESET}")
                    await self._send_cmd(payload)

            elif key in ("\r", "\n"):
                self._rprint("")   # blank line for visual breathing room

            else:
                self._rprint(f"{GREY}[{ts()}] Unknown key: {repr(key)}  (press ? for help){RESET}")

    async def _handle_entry_submit(
        self,
        action: str,
        buffer: str,
        ota_path: str | None,
        stop_event: asyncio.Event,
    ) -> tuple[bool, str, str, str | None]:
        """
        Process a completed text-entry submission.

        Returns (entry_mode, entry_buffer, entry_prefix, ota_path) reflecting
        the new state — either cleared (both prompts done) or advanced to the
        next prompt stage.
        """
        if action == "ota_firmware":
            if os.path.exists(buffer):
                # Path is valid — advance to the password prompt.
                return True, "", "OTA Upload -- Password: ", buffer
            else:
                self._term.set_status("  OTA Failed: File Not Found", timeout=5.0)
                return False, "", "", None

        elif action == "ota_password":
            # Both path and password are collected — kick off the upload.
            asyncio.ensure_future(self._run_ota_upload(ota_path, buffer))
            return False, "", "", None

        return False, "", "", None


    def _print_shortcuts(self) -> None:
        """Print the keyboard shortcut reference table using the supplied print function."""
        self._rprint(f"\n{CYAN}{BOLD}Keyboard shortcuts:{RESET}")
        for key, (_, desc) in SHORTCUTS.items():
            self._rprint(f"  {BOLD}{key}{RESET}  {desc}")
        self._rprint("")


# ── Device discovery ───────────────────────────────────────────────────────────

async def find_device(name_suffix: str = None, address: str = None) -> str:
    """
    Resolve a BLE device address.

    Priority:
    1. Return address directly if provided.
    2. Scan for devices whose names match name_suffix (or "node-" prefix).
    3. Present an interactive menu when multiple candidates are found.

    Returns the BLE MAC address string.
    """
    if address:
        return address

    print(f"{YELLOW}Scanning for ESPHome nodes...{RESET}")
    devices = await BleakScanner.discover(timeout=2.0)
    devices = _filter_devices(devices, name_suffix)

    if not devices:
        # Widen the search with a longer timeout before giving up.
        print(f"{YELLOW}No devices found with initial filter, scanning more broadly...{RESET}")
        devices = await BleakScanner.discover(timeout=10.0)
        devices = _filter_devices(devices, name_suffix)

    if not devices:
        print(f"{RED}No ESPHome nodes found.{RESET}")
        sys.exit(1)

    if len(devices) == 1:
        print(f"{GREEN}Found: {devices[0].name} ({devices[0].address}){RESET}")
        return devices[0].address

    return _prompt_device_selection(devices)


def _filter_devices(devices, name_suffix: str | None) -> list:
    """Return only devices whose names match the expected naming convention."""
    if name_suffix:
        return [d for d in devices if d.name and d.name.lower().endswith(name_suffix.lower())]
    return [d for d in devices if d.name and d.name.lower().startswith("ccsnode-")]


def _prompt_device_selection(devices) -> str:
    """Prompt the user to pick one device from a numbered list; returns its address."""
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
            print(f"{RED}Please enter a number between 1 and {len(devices)}.{RESET}")
        except ValueError:
            print(f"{RED}Invalid input. Please enter a number.{RESET}")

# ── Top-level runner ───────────────────────────────────────────────────────────

async def run(args) -> None:
    """
    Resolve the target device, then connect and reconnect until the user quits.
    Device discovery only happens once; on reconnect the same address is reused.
    """
    address = await find_device(name_suffix=args.name, address=args.address)

    while True:
        print(f"{YELLOW}Connecting to {address}...{RESET}")
        client = BLELoggerClient(address)

        try:
            user_quit = await client.connect_and_run()
        except Exception as e:
            print(f"{RED}Connection error: {e}{RESET}")
            user_quit = False

        if user_quit:
            break

        print(f"{YELLOW}Reconnecting in 3 seconds...{RESET}")
        await asyncio.sleep(3)

    print(f"\r\n{GREEN}Disconnected.{RESET}\n")
    sys.exit(0)


async def main() -> None:
    parser = argparse.ArgumentParser(description="ESPHome BLE Logger")
    parser.add_argument("--name",    "-n", help="Device name suffix to match", default=None)
    parser.add_argument("--address", "-a", help="BLE MAC address to connect to", default=None)
    args = parser.parse_args()

    try:
        await run(args)
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Interrupted.{RESET}")


if __name__ == "__main__":
    asyncio.run(main())