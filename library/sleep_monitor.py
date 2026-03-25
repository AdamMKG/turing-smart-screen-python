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
from library.display import display
from library.log import logger

# How long (seconds) after wake to keep printing health-check diagnostics
_HEALTH_CHECK_DURATION = 30
_HEALTH_CHECK_INTERVAL = 5


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
                # Step 1: Log the state of every thread so we can see what survived sleep
                self._log_thread_states("WAKE-START")

                # Step 2: Flush any stale commands that were queued before sleep.
                self._flush_queue()

                # Step 3: Give the USB serial device time to re-enumerate after
                # the host controller resumes.
                logger.debug("Waiting 3s for USB serial device to stabilise...")
                time.sleep(3)

                # Step 4: Reset the serial port and re-initialize the display
                # protocol.  After suspend the screen's protocol state machine
                # is in an unknown state (it was mid-bitmap when we slept).
                # Just reopening the serial port is not enough — we must
                # re-send HELLO + orientation so the screen accepts new
                # bitmap commands.
                self._reset_serial()
                self._reinitialize_display_protocol()

                # Step 5: Flush the queue a second time (discard dynamic updates
                # that accumulated during the 3s wait).
                self._flush_queue()

                # Step 6: Log thread states again after the serial reset
                self._log_thread_states("WAKE-POST-SERIAL-RESET")

                # Step 7: Restore brightness (bypass_queue — direct serial write)
                logger.info("Restoring screen brightness")
                self._display.turn_on()

                # Step 8: Redraw static content FIRST so dynamic stats layer on top
                logger.info("Redrawing static display content")
                self._display.display_static_images()
                self._display.display_static_text()

                # Step 9: Wait for static content to be fully sent
                logger.info("Waiting for static content to finish drawing...")
                self._wait_for_queue_drain(timeout=15)

                # Step 10: Log thread states after static content is drawn
                self._log_thread_states("WAKE-POST-STATIC-DRAW")

                logger.info(
                    "Display recovery complete — "
                    "dynamic stats will now layer on top of the background"
                )

                # Step 11: Start a background health-check that logs queue size
                # and thread states every few seconds for a short window after
                # wake, so we can see whether dynamic stats are flowing.
                self._start_health_check()

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

    def _log_thread_states(self, label: str):
        """
        Log the name and alive-status of every thread in the process.
        This lets us see whether scheduler stat threads (CPU_Percentage,
        GPU_Stats, Queue_Handler, etc.) survived the sleep/wake transition.
        """
        threads = threading.enumerate()
        alive_names = []
        dead_names = []
        for t in threads:
            if t.is_alive():
                alive_names.append(t.name)
            else:
                dead_names.append(t.name)

        logger.info(
            f"[{label}] Threads alive ({len(alive_names)}): {', '.join(sorted(alive_names))}"
        )
        if dead_names:
            logger.warning(
                f"[{label}] Threads DEAD ({len(dead_names)}): {', '.join(sorted(dead_names))}"
            )

        logger.info(f"[{label}] Queue size: {config.update_queue.qsize()}")

    def _start_health_check(self):
        """
        Spawn a short-lived daemon thread that logs queue size and thread
        states every few seconds after wake.  This tells us whether dynamic
        stat items are flowing into the queue and being processed.
        """

        def _health_loop():
            elapsed = 0.0
            prev_qsize = -1
            while elapsed < _HEALTH_CHECK_DURATION:
                time.sleep(_HEALTH_CHECK_INTERVAL)
                elapsed += _HEALTH_CHECK_INTERVAL
                qsize = config.update_queue.qsize()
                delta = qsize - prev_qsize if prev_qsize >= 0 else 0
                prev_qsize = qsize

                # Count scheduler-related threads that should be alive
                sched_threads = [
                    t
                    for t in threading.enumerate()
                    if t.name
                    in (
                        "CPU_Percentage",
                        "CPU_Frequency",
                        "CPU_Load",
                        "CPU_FanSpeed",
                        "GPU_Stats",
                        "Memory_Stats",
                        "Disk_Stats",
                        "Net_Stats",
                        "Date_Stats",
                        "SystemUptime_Stats",
                        "Custom_Stats",
                        "Weather_Stats",
                        "Ping_Stats",
                        "Queue_Handler",
                    )
                ]
                alive = [t.name for t in sched_threads if t.is_alive()]
                dead = [t.name for t in sched_threads if not t.is_alive()]

                # Check if the update_queue_mutex is stuck (deadlocked)
                mutex_status = "UNKNOWN"
                try:
                    lcd = display.lcd
                    if lcd is not None and hasattr(lcd, "update_queue_mutex"):
                        got_lock = lcd.update_queue_mutex.acquire(timeout=0.5)
                        if got_lock:
                            lcd.update_queue_mutex.release()
                            mutex_status = "FREE"
                        else:
                            mutex_status = "LOCKED/DEADLOCKED"
                    else:
                        mutex_status = "N/A"
                except Exception as e:
                    mutex_status = f"ERROR: {e}"

                logger.info(
                    f"[HEALTH +{elapsed:.0f}s] Queue size: {qsize} (delta: {delta:+d}) | "
                    f"Scheduler threads alive: {len(alive)}/{len(alive) + len(dead)} | "
                    f"update_queue_mutex: {mutex_status}"
                )
                if dead:
                    logger.warning(
                        f"[HEALTH +{elapsed:.0f}s] DEAD scheduler threads: {', '.join(sorted(dead))}"
                    )
                if mutex_status == "LOCKED/DEADLOCKED":
                    logger.error(
                        f"[HEALTH +{elapsed:.0f}s] update_queue_mutex appears deadlocked! "
                        "All stat threads are likely blocked waiting to acquire it."
                    )

            logger.info("[HEALTH] Post-wake health check complete")

        t = threading.Thread(target=_health_loop, name="WakeHealthCheck", daemon=True)
        t.start()

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

    def _reinitialize_display_protocol(self):
        """
        Re-send the display protocol initialization commands (HELLO +
        orientation) so the screen's internal state machine is ready to
        accept bitmap updates again.

        After suspend the screen was mid-way through processing a bitmap
        command.  Simply reopening the serial port gives us a clean byte
        stream, but the screen still thinks it's in the middle of that
        old command.  Re-running InitializeComm sends HELLO which resets
        the screen's command parser, and SetOrientation puts it back into
        the correct rendering mode.
        """
        try:
            logger.info("Re-initializing display protocol (HELLO + orientation)...")
            self._display.lcd.InitializeComm()

            from library import config
            from library.lcd.lcd_comm import Orientation

            # Determine the correct orientation from theme config
            orientation_str = config.THEME_DATA["display"].get(
                "DISPLAY_ORIENTATION", "portrait"
            )
            reverse = config.CONFIG_DATA["display"].get("DISPLAY_REVERSE", False)

            if orientation_str == "portrait":
                orientation = (
                    Orientation.REVERSE_PORTRAIT if reverse else Orientation.PORTRAIT
                )
            elif orientation_str == "landscape":
                orientation = (
                    Orientation.REVERSE_LANDSCAPE if reverse else Orientation.LANDSCAPE
                )
            else:
                orientation = Orientation.PORTRAIT

            self._display.lcd.SetOrientation(orientation=orientation)
            logger.info("Display protocol re-initialized successfully")
        except Exception as e:
            logger.error(f"Failed to re-initialize display protocol on wake: {e}")

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
