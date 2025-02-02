# Copyright 2015-2021 Espressif Systems (Shanghai) CO LTD
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
import queue
import threading
import termios

from typing import Optional, Tuple, Any

from serial.tools.miniterm import Console

from .console_parser import ConsoleParser
from .constants import CMD_STOP, TAG_CMD, CTRL_RBRACKET, CTRL_C
from .stoppable_thread import StoppableThread

logger = logging.getLogger(__name__)


class ConsoleReader(StoppableThread):
    """
    Read input keys from the console and push them to the queue, until stopped.

    Handles:
    - Regular character input processing
    - Control characters (Ctrl+C, Ctrl+])
    - Keyboard interrupts
    - Cross-platform input buffer flushing - necessary because the console is borrowed from the miniterm module
    - Graceful cleanup on exit
    """

    def __init__(
        self,
        console: Console,
        event_queue: queue.Queue,
        cmd_queue: queue.Queue,
        parser: ConsoleParser,
        test_mode: bool,
    ) -> None:
        """
        Initialize the console reader.

        Args:
            console: Console instance for input/output
            event_queue: Queue for event messages
            cmd_queue: Queue for command messages
            parser: Parser for console input
            test_mode: For running in test mode
        """
        super().__init__()
        self.console = console
        self.event_queue = event_queue
        self.cmd_queue = cmd_queue
        self.parser = parser
        self.test_mode = test_mode
        self.stop_event = threading.Event()
        self.input_buffer = queue.Queue(maxsize=1000)

    def __enter__(self) -> "ConsoleReader":
        """Setup console when entering context."""
        self.console.setup()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Cleanup console when exiting context."""
        self._flush_input()
        self.console.cleanup()

    def run(self) -> None:
        """Main loop that reads and processes console input."""
        self.console.setup()

        try:
            while not self.stop_event.is_set():
                try:
                    c = self.console.getkey()
                    self._handle_input(c)
                except KeyboardInterrupt:
                    self._handle_stop()
                    break
                except Exception as e:
                    logger.error("Exception in console reader: %s", e)
                    break
        finally:
            self._flush_input()
            self.console.cleanup()

    def _handle_input(self, char: Optional[str]) -> None:
        """
        Process a single character of input.

        Args:
            char: Input character to process
        """
        if char in (CTRL_C, CTRL_RBRACKET):
            self._handle_stop()
            return

        if char is not None:
            try:
                self.input_buffer.put_nowait(char)
                ret = self.parser.parse(char)
                if ret is not None:
                    self._handle_parser_result(ret)
            except queue.Full:
                logger.warning("Input buffer full, discarding input")
                self._process_buffer()

    def _handle_parser_result(self, result: Tuple[str, Any]) -> None:
        """
        Handle parser result and route to appropriate queue.

        Args:
            result: Tuple of (tag, command) from parser
        """
        tag, cmd = result
        target_queue = (
            self.cmd_queue if tag == TAG_CMD and cmd != CMD_STOP else self.event_queue
        )
        target_queue.put(result)

    def _handle_stop(self) -> None:
        """Handle stop event in a consistent way."""
        self.event_queue.put((TAG_CMD, CMD_STOP))
        self.stop_event.set()

    def _flush_input(self) -> bool:
        """
        Flush pending console input.

        Returns:
            bool: True if successful, False otherwise
        """
        if os.name == "posix":
            return self._flush_posix()
        elif os.name == "nt":
            return self._flush_windows()
        return False

    def _flush_posix(self) -> bool:
        """
        Flush input buffer on POSIX systems.

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            if hasattr(self.console, "fd"):
                fd = self.console.fd
                if os.isatty(fd):
                    termios.tcflush(fd, termios.TCIFLUSH)
                    return True
        except Exception as e:
            logger.warning("Failed to flush POSIX console: %s", e)
        return False

    def _flush_windows(self) -> bool:
        """
        Flush input buffer on Windows systems.

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            import msvcrt

            while msvcrt.kbhit():
                msvcrt.getch()
            return True
        except Exception as e:
            logger.warning("Failed to flush Windows console: %s", e)
        return False

    def _process_buffer(self) -> None:
        """Process and clear input buffer."""
        try:
            while not self.input_buffer.empty():
                self.input_buffer.get_nowait()
        except queue.Empty:
            pass

    def _cancel(self) -> None:
        """
        Cancel the console reader operation.
        Ensures pending input is flushed before stopping.
        """
        if not self.stop_event.is_set():
            self.stop_event.set()
            if not self.test_mode:
                if not self._flush_input():
                    self._send_interrupt()

    def _send_interrupt(self) -> None:
        """Send interrupt signal to console."""
        try:
            if hasattr(self.console, "fd"):
                os.write(self.console.fd, b"\x03")
        except Exception as e:
            logger.error("Failed to send interrupt: %s", e)
