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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable

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


# ── Helpers ────────────────────────────────────────────────────────────────────

def ts() -> str:
    """Return current time as HH:MM:SS.mmm string."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


# ── Key binding ────────────────────────────────────────────────────────────────

KeyCallback = Callable[[str], "Awaitable[None] | None"]

@dataclass
class KeyBinding:
    key: str
    callback: KeyCallback
    description: str = ""
    hidden: bool = False   # omit from the ? help listing


# ── Raw terminal context manager ───────────────────────────────────────────────

class RawTerminal:
    """
    Context manager that puts stdin into raw (single-keypress) mode while active,
    then restores the previous terminal settings on exit.

    Only acts when stdin is a real TTY; safe to use in piped contexts.
    """
    def __init__(self):
        self._read_cancelled = False

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

    # ── Output ─────────────────────────────────────────────────────────────────

    def rprint(self, *args, **kwargs) -> None:
        """Print with \\r\\n line endings (required in raw-terminal mode)."""
        buf = io.StringIO()
        print(*args, file=buf, **kwargs)
        sys.stdout.write(buf.getvalue().replace("\n", "\r\n"))
        sys.stdout.flush()

    # ── Key reading ────────────────────────────────────────────────────────────

    def _blocking_read_stdin(self) -> str | None:
        """
        Block in a polling loop (50 ms ticks) until a character arrives on stdin,
        returning it — or None if _read_cancelled is set externally.

        Uses os.read() on the raw fd rather than sys.stdin.read() to bypass
        Python's internal read buffer, which can cause select() to block even
        when characters are available (they're buffered in Python, not the kernel).
        """
        fd = sys.stdin.fileno()
        while True:
            ready = select.select([fd], [], [], 0.05)[0]
            if ready:
                return os.read(fd, 1).decode("utf-8", errors="replace")
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
            self._read_cancelled = True
            return None

        raw_key = key_future.result()

        # Discard ANSI escape sequences (arrow keys, function keys, etc.)
        if raw_key == "\x1b":
            ready = select.select([sys.stdin], [], [], 0.05)[0]
            if ready:
                seq = sys.stdin.read(1)
                if seq == "[":
                    sys.stdin.read(1)
                return None
            return "\x1b"

        return raw_key


# ── Status bar ─────────────────────────────────────────────────────────────────

class RawTerminalWithStatusBar(RawTerminal):
    """
    Manages a persistent status bar rendered on the current terminal line using
    in-place ANSI overwrites.

    Key bindings are registered with bind(); the interactive loop is driven by
    run_keyboard_loop().  Multi-line text entry is handled by the prompt() coroutine,
    which suspends normal key dispatch while the user types a value.

    Usage:
        with RawTerminalWithStatusBar(connected_name=..., connected_address=...) as term:
            term.bind("q", my_quit_handler, "Quit")
            term.bind("p", my_ping_handler, "Ping device")
            await term.run_keyboard_loop(stop_event)
    """

    def __init__(self, connected_name: str = None, connected_address: str = None) -> None:
        super().__init__()
        self.connected_name    = connected_name
        self.connected_address = connected_address

        self._message: str | None    = None
        self._input_mode: bool       = False
        self._timeout_handle         = None

        self._bindings: dict[str, KeyBinding] = {}
        self._default_handler: KeyCallback | None = None

    def __exit__(self, *_) -> None:
        self.clear()
        super().__exit__(*_)
        self.clear()

    # ── Binding registration ───────────────────────────────────────────────────

    def bind(
        self,
        key: str,
        callback: KeyCallback,
        description: str = "",
        *,
        hidden: bool = False,
    ) -> None:
        """Register a callback for a single keypress.

        The callback may be a plain function or an async coroutine function;
        both are supported transparently by run_keyboard_loop.
        """
        self._bindings[key] = KeyBinding(key, callback, description, hidden)

    def on_unhandled_key(self, callback: KeyCallback) -> None:
        """Register a fallback handler for keys that have no explicit binding."""
        self._default_handler = callback

    def print_bindings(self) -> None:
        """Print all non-hidden bindings as a help table."""
        self.rprint(f"\n{CYAN}{BOLD}Keyboard shortcuts:{RESET}")
        for kb in self._bindings.values():
            if not kb.hidden:
                self.rprint(f"  {BOLD}{kb.key}{RESET}  {kb.description}")
        self.rprint("")

    # ── Text-entry prompt ──────────────────────────────────────────────────────

    async def prompt(self, prefix: str, *, stop_event: asyncio.Event) -> str | None:
        """
        Display an inline text-entry prompt in the status bar and collect a
        line of input, returning the string when the user presses Enter.

        Returns None if the user presses ESC or stop_event fires during entry.

        Calls read_key directly rather than relying on run_keyboard_loop to
        feed keys — the loop is blocked awaiting this coroutine, so it cannot
        read keys concurrently.
        """
        buffer = ""
        self.set_status(prefix, input_mode=True)

        try:
            while not stop_event.is_set():
                chunk = await self.read_key(stop_event)

                if chunk is None:               # stop_event fired
                    return None
                elif chunk in ("\r", "\n"):
                    return buffer
                elif chunk == "\x7f":           # backspace
                    buffer = buffer[:-1]
                elif chunk == "\x1b":           # ESC — cancel
                    return None
                elif chunk.isprintable():
                    buffer += chunk

                self.set_status(f"{prefix}{buffer}", input_mode=True)

        finally:
            self.set_status(None)

    # ── Keyboard loop ──────────────────────────────────────────────────────────

    async def run_keyboard_loop(self, stop_event: asyncio.Event) -> None:
        """
        Read keypresses and dispatch them to registered callbacks until
        stop_event is set or the loop is broken by a quit binding.
        """
        while not stop_event.is_set():
            key = await self.read_key(stop_event)
            if key is None:
                continue

            if key in ("\r", "\n"):
                self.rprint("")   # breathing room
                continue

            binding = self._bindings.get(key)
            if binding is not None:
                result = binding.callback(key)
                if asyncio.iscoroutine(result):
                    await result
            elif self._default_handler is not None:
                result = self._default_handler(key)
                if asyncio.iscoroutine(result):
                    await result

    # ── Status bar public API ──────────────────────────────────────────────────

    def set_status(
        self,
        message: str | None,
        *,
        input_mode: bool = False,
        timeout: float | None = None,
    ) -> None:
        """
        Set the status bar text.

        Args:
            message:    Text to display, or None to revert to the default status.
            input_mode: Position the cursor at the end so the user can see input.
            timeout:    Automatically revert to default status after this many seconds.
        """
        self._message    = message
        self._input_mode = input_mode
        self.render()

        if self._timeout_handle is not None:
            self._timeout_handle.cancel()
            self._timeout_handle = None

        if timeout is not None:
            self._timeout_handle = asyncio.get_event_loop().call_later(
                timeout, lambda: self.set_status(None)
            )

    def clear(self) -> None:
        """Erase the status bar line."""
        cols = os.get_terminal_size().columns
        sys.stdout.write(f"\r{' ' * cols}\r")
        sys.stdout.flush()

    def render(self) -> None:
        """Redraw the status bar in place on the current line."""
        cols = os.get_terminal_size().columns
        msg  = self._build_message(cols)

        sys.stdout.write(f"\r\033[2K{WHITE_ON_BLUE}{msg}{RESET}")

        if self._input_mode:
            # \033[{n}G moves to absolute column n (1-based) — reliable even if
            # a concurrent rprint left the cursor somewhere unexpected.
            sys.stdout.write(f"\033[{len(msg) + 1}G")
        else:
            sys.stdout.write("\r")

        sys.stdout.flush()

    def rprint(self, *args, **kwargs) -> None:
        """Clear and redraw the status bar around RawTerminal's rprint."""
        self.clear()
        super().rprint(*args, **kwargs)
        self.render()

    # ── Internal ───────────────────────────────────────────────────────────────

    def _build_message(self, cols: int) -> str:
        if self._message is not None:
            msg = self._message
        elif self.connected_name:
            prefix = f"  Connected > {self.connected_name} [{self.connected_address}]"
            suffix = "[?] help "
            padding = max(0, cols - len(prefix) - len(suffix))
            msg = f"{prefix}{' ' * padding}{suffix}"
        else:
            msg = "  Disconnected.   [?] help "

        # Truncate (from the left, to preserve the tail) or pad to exactly cols - 1
        # so the highlight fills the full terminal width without wrapping.
        if len(msg) > cols - 1:
            msg = msg[-(cols - 1):]
        else:
            msg = msg + " " * (cols - 1 - len(msg))

        return msg


# ── BLE client wrapper ─────────────────────────────────────────────────────────

class BLELoggerClient:
    """
    Wraps a BleakClient and owns all application-level state for one BLE session.

    Keyboard bindings are declared in _register_bindings() and executed by the
    terminal's run_keyboard_loop(); there is no bespoke key-dispatch logic here.
    """

    def __init__(self, address: str) -> None:
        self.address       = address
        self._client: BleakClient | None = None
        self._term: RawTerminalWithStatusBar | None = None
        self._user_quit: bool = False
        self._stop_event: asyncio.Event | None = None

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
        self._stop_event = stop_event
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
                return True

            print(f"{YELLOW}Subscribing to log characteristic...{RESET}")
            await client.start_notify(LOG_CHAR_UUID, self._on_log_notification)

            print(f"{YELLOW}Subscribing to state characteristic...{RESET}")
            await client.start_notify(STATE_CHAR_UUID, self._on_state_notification)

            print(f"{GREEN}Ready. Press ? for keyboard shortcuts.{RESET}")

            with RawTerminalWithStatusBar(
                connected_name=client.name,
                connected_address=client.address,
            ) as term:
                self._term = term
                self._register_bindings(term, stop_event)
                term.print_bindings()
                await term.run_keyboard_loop(stop_event)

            await asyncio.sleep(0.1)

            if self._user_quit:
                print(f"\n{YELLOW}Disconnecting...{RESET}")
                await client.stop_notify(LOG_CHAR_UUID)
                await client.stop_notify(STATE_CHAR_UUID)
                return True

        self._client = None
        return self._user_quit

    def _verify_service(self) -> bool:
        return any(
            s.uuid.lower() == SERVICE_UUID.lower()
            for s in self._client.services
        )

    def _make_disconnected_callback(self, stop_event: asyncio.Event):
        def _callback(client: BleakClient) -> None:
            self._rprint(f"\n{RED}[{ts()}] Bluetooth connection lost.{RESET}")
            self._term.clear()
            stop_event.set()
        return _callback

    # ── Binding registration ───────────────────────────────────────────────────

    def _register_bindings(
        self, term: RawTerminalWithStatusBar, stop_event: asyncio.Event
    ) -> None:
        """Declare all keyboard shortcuts for this session."""

        term.bind("q",  self._on_quit,                    "Disconnect and quit")
        term.bind("\x03", self._on_quit, hidden=True)     # Ctrl-C alias

        term.bind("?",  lambda _: term.print_bindings(),  "Show keyboard shortcuts")

        # Simple fire-and-forget commands — use the _cmd() factory.
        term.bind("c",  self._cmd("log_config"),           "Request log_config from device")
        term.bind("p",  self._cmd("ping"),                 "Send ping command to device")
        term.bind("s",  self._cmd("update_sensors"),       "Request immediate sensor update")
        term.bind("r",  self._cmd("restart"),              "Soft-restart the device")

        # OTA needs interactive prompts — handled in a dedicated coroutine.
        term.bind("f",  self._on_ota,                      "OTA firmware upgrade")

        term.on_unhandled_key(self._on_unknown_key)

    # ── Output helper ──────────────────────────────────────────────────────────

    def _rprint(self, *args, **kwargs) -> None:
        if self._term:
            self._term.rprint(*args, **kwargs)
        else:
            buf = io.StringIO()
            print(*args, file=buf, **kwargs)
            sys.stdout.write(buf.getvalue().replace("\n", "\r\n"))
            sys.stdout.flush()

    # ── Key handlers ──────────────────────────────────────────────────────────

    async def _on_quit(self, _key: str) -> None:
        self._rprint(f"\n{YELLOW}Quit requested.{RESET}")
        self._user_quit = True
        self._stop_event.set()

    async def _on_ota(self, _key: str) -> None:
        """Prompt for firmware path and password, then kick off the OTA upload."""
        path = await self._term.prompt(
            "OTA Upload -- firmware path (.ota.bin): ",
            stop_event=self._stop_event,
        )
        if not path:
            self._term.set_status("  OTA cancelled.", timeout=3.0)
            return

        if not os.path.exists(path):
            self._term.set_status("  OTA Failed: File Not Found", timeout=5.0)
            return

        password = await self._term.prompt(
            "OTA Upload -- Password: ",
            stop_event=self._stop_event,
        )
        if password is None:
            self._term.set_status("  OTA cancelled.", timeout=3.0)
            return

        asyncio.ensure_future(self._run_ota_upload(path, password))

    def _on_unknown_key(self, key: str) -> None:
        self._term.set_status(f"  Unknown Key: {repr(key)} (press ? for help)", timeout=3.0)

    def _cmd(self, payload: str) -> KeyCallback:
        """Return an async key callback that writes payload to the command characteristic."""
        async def handler(_key: str) -> None:
            await self._send_cmd(payload)
        return handler

    # ── Notification callbacks ─────────────────────────────────────────────────

    def _on_log_notification(
        self, characteristic: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        try:
            msg = data.decode("utf-8", errors="replace").strip()

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
        try:
            msg   = data.decode("utf-8", errors="replace").strip()
            parts = msg.split("|")

            self._rprint(f"\n{CYAN}{'─' * 60}{RESET}")
            self._rprint(f"{GREEN}[{ts()}] SENSOR UPDATE{RESET}")

            if len(parts) >= 8:
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
        water_total = float(raw_total)
        uptime      = float(raw_uptime)

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
        try:
            await self._client.write_gatt_char(
                CMD_CHAR_UUID, payload.encode("utf-8"), response=True
            )
            self._rprint(f"{GREEN}[{ts()}] CMD sent: {payload}{RESET}")
        except Exception as e:
            self._rprint(f"{RED}[{ts()}] Failed to send cmd '{payload}': {e}{RESET}")

    # ── OTA firmware upload ────────────────────────────────────────────────────

    async def _run_ota_upload(self, path: str, password: str) -> None:
        try:
            with open(path, "rb") as f:
                firmware = f.read()

            uploader = esp32_ble_ota_upload.ESP32BLEOTAUploader(
                firmware=firmware, password=password
            )
            await uploader.upload(self._client, on_progress=self._on_ota_progress)
            self._term.set_status("  OTA Upload Complete")

        except FileNotFoundError:
            self._term.set_status("  OTA Failed: File Not Found", timeout=5.0)
        except ValueError as e:
            self._rprint(f"{RED}[OTA] OTA Failed: {e}{RESET}")
            self._term.set_status(f"  OTA Failed: {e}", timeout=5.0)

    def _on_ota_progress(self, pct: int, rate: float, elapsed: float, eta: float) -> None:
        filled   = "█" * (pct // 5)
        unfilled = "░" * (20 - pct // 5)
        self._term.set_status(f"  OTA [{filled}{unfilled}] {pct:3d}%  {rate:4.1f} KB/s  ETA {int(eta)}s")


# ── Device discovery ───────────────────────────────────────────────────────────

async def find_device(name_suffix: str = None, address: str = None) -> str:
    if address:
        return address

    print(f"{YELLOW}Scanning for ESPHome nodes...{RESET}")
    devices = await BleakScanner.discover(timeout=2.0)
    devices = _filter_devices(devices, name_suffix)

    if not devices:
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
    if name_suffix:
        return [d for d in devices if d.name and d.name.lower().endswith(name_suffix.lower())]
    return [d for d in devices if d.name and d.name.lower().startswith("ccsnode-")]


def _prompt_device_selection(devices) -> str:
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