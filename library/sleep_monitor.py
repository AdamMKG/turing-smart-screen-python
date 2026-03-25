# turing-smart-screen-python - a Python system monitor and library for USB-C displays like Turing Smart Screen or XuanFang
# https://github.com/mathoudebine/turing-smart-screen-python/

# Sleep/Wake monitor for Linux using D-Bus (systemd-logind)
# Listens for the PrepareForSleep signal from org.freedesktop.login1.Manager
# - On sleep (PrepareForSleep=true):  dims the screen to brightness 0
# - On wake  (PrepareForSleep=false): flushes stale queue, waits for USB to stabilise,
#                                      restores brightness and redraws static content

import queue
import threading
import time

import library.config as config
from library.log import logger


class SleepMonitor:
    """Monitors D-Bus for system sleep/wake events and controls display brightness accordingly."""

    def __init__(self, display):
        self._display = display
        self._thread = None
        self._loop = None
        self._running = False

    def start(self):
        """Start the D-Bus sleep monitor in a background daemon thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="SleepMonitor",
            daemon=True,
        )
        self._thread.start()
        logger.info("Linux sleep/wake monitor started (D-Bus PrepareForSleep listener)")

    def stop(self):
        """Stop the D-Bus sleep monitor and quit the GLib main loop."""
        self._running = False
        if self._loop is not None:
            try:
                self._loop.quit()
            except Exception:
                pass
        logger.info("Linux sleep/wake monitor stopped")

    def _flush_queue(self):
        """
        Drain any stale display commands that were queued before sleep.
        During suspend the scheduler threads freeze mid-cycle; when they resume
        the queue may contain partially-written or outdated serial commands that
        would block or corrupt the display if processed after wake.
        """
        flushed = 0
        try:
            while True:
                config.update_queue.get_nowait()
                flushed += 1
        except queue.Empty:
            pass
        if flushed:
            logger.info(f"Flushed {flushed} stale item(s) from the display queue")
        else:
            logger.debug("Display queue was already empty on wake")

    def _on_prepare_for_sleep(self, going_to_sleep):
        """
        Callback invoked by D-Bus when the system is about to sleep or has just woken up.

        Args:
            going_to_sleep: boolean - True if suspending, False if resuming.
        """
        if going_to_sleep:
            logger.info("System is going to sleep — dimming screen to 0 brightness")
            try:
                self._display.turn_off()
            except Exception as e:
                logger.error(f"Failed to dim screen on sleep: {e}")
        else:
            logger.info("System is waking up — beginning display recovery sequence")
            try:
                # Step 1: Flush any stale commands that were queued before sleep.
                # The scheduler threads were frozen by the kernel and may have left
                # partial or outdated serial writes in the queue.
                self._flush_queue()

                # Step 2: Give the USB serial device time to re-enumerate after
                # the host controller resumes.  Without this pause the serial
                # writes below can fail or silently drop data.
                logger.debug("Waiting 3s for USB serial device to stabilise...")
                time.sleep(3)

                # Step 3: Reset the serial port to clear any corrupted state
                # left over from the suspend.  The kernel may have power-cycled
                # the USB host controller, leaving stale bytes in the buffer.
                self._reset_serial()

                # Step 4: Flush the queue a second time.  During the 3s wait the
                # scheduler threads (1-second intervals for CPU%, GPU, DATE etc.)
                # will have already pushed dynamic updates into the queue.  If we
                # let those through before the static content they'll be drawn and
                # then immediately buried under the full-screen background image.
                self._flush_queue()

                # Step 5: Restore brightness (uses bypass_queue so goes straight
                # to the serial port, not through the update queue).
                logger.info("Restoring screen brightness")
                self._display.turn_on()

                # Step 6: Redraw static content FIRST (background image + header
                # text).  These go through the queue and must be processed before
                # any dynamic stats so that the stats draw on top of the
                # background rather than being overwritten by it.
                logger.info("Redrawing static display content")
                self._display.display_static_images()
                self._display.display_static_text()

                # Step 7: Wait for the static content to be fully sent through
                # the queue before we allow dynamic stats to pile on top.
                # The background image is 480x1920 on the 8.8" screen and takes
                # a noticeable amount of time to transmit over serial.
                logger.info("Waiting for static content to finish drawing...")
                self._wait_for_queue_drain(timeout=15)

                logger.info(
                    "Display recovery complete — "
                    "dynamic stats will now layer on top of the background"
                )
            except Exception as e:
                logger.error(f"Failed to restore screen on wake: {e}")

    def _wait_for_queue_drain(self, timeout: int = 15):
        """
        Block until the update queue is empty or the timeout expires.
        This ensures that the static content (background image + headers)
        has been fully transmitted to the screen before the scheduler
        threads start pushing dynamic stat updates on top.
        """
        waited = 0.0
        while not config.update_queue.empty() and waited < timeout:
            time.sleep(0.2)
            waited += 0.2
        if waited >= timeout:
            logger.warning(
                f"Queue did not drain within {timeout}s "
                f"({config.update_queue.qsize()} items remaining)"
            )
        else:
            logger.debug(f"Queue drained in {waited:.1f}s")

    def _reset_serial(self):
        """
        Close and reopen the serial port to clear any corrupted state from
        suspend.  The USB host controller may have been power-cycled by the
        kernel, leaving the pyserial file descriptor pointing at a stale
        device.  Flushing the input buffer after reopening ensures we don't
        read back garbage bytes that would confuse the command protocol.
        """
        try:
            lcd = self._display.lcd
            if lcd is None:
                logger.warning("Cannot reset serial port — lcd object is None")
                return

            logger.info("Resetting serial port (close → reopen → flush)...")

            # Close the existing (possibly stale) connection
            lcd.closeSerial()
            time.sleep(0.5)

            # Reopen the serial port fresh
            lcd.openSerial()

            # Flush any garbage left in the input buffer
            lcd.serial_flush_input()

            logger.info("Serial port reset complete")
        except Exception as e:
            logger.error(f"Failed to reset serial port on wake: {e}")

    def _monitor_loop(self):
        """
        Background thread entry point.
        Connects to the system D-Bus and listens for PrepareForSleep signals
        from org.freedesktop.login1.Manager using the GLib main loop.
        """
        try:
            import dbus
            from dbus.mainloop.glib import DBusGMainLoop
            from gi.repository import GLib

            # Set up D-Bus GLib integration for this thread
            DBusGMainLoop(set_as_default=True)

            system_bus = dbus.SystemBus()

            # Subscribe to the PrepareForSleep signal from systemd-logind
            system_bus.add_signal_receiver(
                handler_function=self._on_prepare_for_sleep,
                signal_name="PrepareForSleep",
                dbus_interface="org.freedesktop.login1.Manager",
                bus_name="org.freedesktop.login1",
                path="/org/freedesktop/login1",
            )

            logger.debug("D-Bus signal receiver registered for PrepareForSleep")

            # Run the GLib main loop to process D-Bus signals
            self._loop = GLib.MainLoop()
            self._loop.run()

        except ImportError as e:
            logger.warning(
                f"Could not start sleep/wake monitor — missing dependency: {e}. "
                "Install dbus-python and PyGObject (pip install dbus-python PyGObject) "
                "for automatic screen dimming on sleep/wake."
            )
        except Exception as e:
            logger.error(f"Sleep/wake monitor encountered an error: {e}")
        finally:
            self._running = False
            logger.debug("Sleep/wake monitor thread exiting")
