"""Local native lifecycle evidence without request bodies or credential values."""
import faulthandler
import os
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

_active = None


def event(name):
    if _active is not None:
        _active.write(name)


class NativeDiagnostics:
    def __init__(self, directory, version):
        self.directory = Path(directory)
        self.version = version
        self.stream = None
        self.lock = threading.Lock()

    def write(self, name):
        with self.lock:
            if self.stream is not None:
                self.stream.write(datetime.now(timezone.utc).isoformat()+" "+name+"\n")
                self.stream.flush()

    def __enter__(self):
        global _active
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.stream = (self.directory / (datetime.now().strftime('%Y%m%d-%H%M%S')+"-"+uuid4().hex[:8]+".log")).open('a', encoding='utf-8')
        except OSError:
            return self  # Diagnostics must never prevent normal app startup.
        self.owns_fault_handler = not faulthandler.is_enabled()
        if self.owns_fault_handler:
            faulthandler.enable(file=self.stream, all_threads=True)
        from PySide6.QtCore import qInstallMessageHandler
        # Qt's callback must remain strongly owned until Qt is no longer used.
        # Store category/location only: arbitrary warning strings can include
        # user text or device paths. Fatal Python/native stacks have no locals.
        def qt_message(kind, context, message):
            self.write('qt '+str(kind)+' function='+str(context.function)+' line='+str(context.line))
            if self.previous_qt_handler:
                self.previous_qt_handler(kind, context, message)
        self.qt_handler = qt_message
        self.previous_qt_handler = qInstallMessageHandler(self.qt_handler)
        self.previous_exception_hook = sys.excepthook
        def exception_hook(kind, value, tb):
            self.write('python '+kind.__name__)
            # Extract frame positions rather than exception values or source
            # statements, which can embed account inputs in diagnostic output.
            for frame in traceback.extract_tb(tb):
                self.write('frame '+frame.filename+':'+str(frame.lineno)+' '+frame.name)
        sys.excepthook = exception_hook
        _active = self
        self.write('start version='+self.version+' pid='+str(os.getpid()))
        return self

    def __exit__(self, *exc):
        global _active
        if self.stream is None:
            return
        self.write('shutdown')
        _active = None
        from PySide6.QtCore import qInstallMessageHandler
        qInstallMessageHandler(self.previous_qt_handler)
        sys.excepthook = self.previous_exception_hook
        if self.owns_fault_handler:
            faulthandler.disable()
        self.stream.close()
        self.stream = None
