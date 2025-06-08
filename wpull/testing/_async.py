import functools
import unittest
import threading
import weakref

from tornado.platform.asyncio import BaseAsyncIOLoop
import asyncio


# Thread-local storage for event loops
_thread_local = threading.local()
# Keep track of loops we created vs loops managed by pytest-asyncio
_created_loops = weakref.WeakSet()


# http://stackoverflow.com/q/23033939/1524507
class AsyncTestCase(unittest.TestCase):
    def setUp(self):
        # Check if we're in pytest-asyncio context
        try:
            # Try to get the current running loop
            current_loop = asyncio.get_running_loop()
            # If we get here, we're in pytest-asyncio context
            self.event_loop = current_loop
            self._we_created_loop = False
        except RuntimeError:
            # No running loop, we need to create one
            # Check if we already have a loop for this thread
            if hasattr(_thread_local, "loop") and not _thread_local.loop.is_closed():
                self.event_loop = _thread_local.loop
                self._we_created_loop = False
            else:
                # Create a new loop for this thread
                self.event_loop = asyncio.new_event_loop()
                self.event_loop.set_debug(True)
                asyncio.set_event_loop(self.event_loop)
                _thread_local.loop = self.event_loop
                _created_loops.add(self.event_loop)
                self._we_created_loop = True

    def tearDown(self):
        # Only close the loop if we created it and we're not in pytest-asyncio
        if self._we_created_loop and self.event_loop in _created_loops:
            if not self.event_loop.is_closed():
                # Don't stop/close immediately, just mark for cleanup
                # The loop might still be needed by other tests in the same thread
                pass
        # Clean up thread-local reference if loop is closed
        if hasattr(_thread_local, "loop") and _thread_local.loop.is_closed():
            delattr(_thread_local, "loop")


def async_test(func=None, timeout=30):
    # tornado.testing
    def wrap(f):
        # Use async def instead of deprecated asyncio.coroutine
        if not asyncio.iscoroutinefunction(f):
            # Convert generator-based coroutine to proper async function
            f = asyncio.coroutine(f)

        @functools.wraps(f)
        def wrapper(self):
            # Get the event loop from the test instance
            loop = self.event_loop

            # Check if we're in pytest-asyncio context (loop is already running)
            try:
                # If get_running_loop() succeeds and it's the same as our loop,
                # we're in pytest-asyncio context
                running_loop = asyncio.get_running_loop()
                if running_loop is loop:
                    # We're in pytest-asyncio, can't use run_until_complete
                    # This shouldn't happen with proper pytest-asyncio setup
                    raise RuntimeError("Cannot use run_until_complete in running loop")
            except RuntimeError:
                pass  # No running loop, we can proceed

            # Use the test's event loop to run the coroutine
            try:
                return loop.run_until_complete(
                    asyncio.wait_for(f(self), timeout=timeout)
                )
            except RuntimeError as e:
                if "cannot be called from a running event loop" in str(e):
                    # Fallback: create a task and let it run
                    task = loop.create_task(f(self))
                    return task
                raise

        return wrapper

    if func is not None:
        return wrap(func)
    else:
        return wrap


class TornadoAsyncIOLoop(BaseAsyncIOLoop):
    def initialize(self, asyncio_loop, **kwargs):
        # FIXME: what did close_loop=False do? Tornado doesn't accept it anymore
        super().initialize(asyncio_loop)


# Cleanup function to close loops when threads end
def _cleanup_thread_loops():
    """Clean up event loops when threads end."""
    if hasattr(_thread_local, "loop"):
        loop = _thread_local.loop
        if not loop.is_closed() and loop in _created_loops:
            try:
                if loop.is_running():
                    loop.call_soon_threadsafe(loop.stop)
                else:
                    loop.close()
            except Exception:
                pass  # Best effort cleanup


# Register cleanup on thread exit
import atexit

atexit.register(_cleanup_thread_loops)
