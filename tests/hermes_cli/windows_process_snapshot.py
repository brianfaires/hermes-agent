"""Query-only diagnostics for a disposable Windows child, never a containment oracle.

WinAPI layout and lifetime contracts:
https://learn.microsoft.com/windows/win32/api/processsnapshot/ns-processsnapshot-pss_thread_entry
https://learn.microsoft.com/windows/win32/api/processsnapshot/nf-processsnapshot-pssfreesnapshot
"""


def thread_suspend_counts(proc):
    import ctypes
    from ctypes import wintypes

    class ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("ExitStatus", wintypes.DWORD), ("TebBaseAddress", ctypes.c_void_p),
            ("ProcessId", wintypes.DWORD), ("ThreadId", wintypes.DWORD),
            ("AffinityMask", ctypes.c_size_t), ("Priority", ctypes.c_int),
            ("BasePriority", ctypes.c_int), ("LastSyscallFirstArgument", ctypes.c_void_p),
            ("LastSyscallNumber", wintypes.WORD), ("CreateTime", wintypes.FILETIME),
            ("ExitTime", wintypes.FILETIME), ("KernelTime", wintypes.FILETIME),
            ("UserTime", wintypes.FILETIME), ("Win32StartAddress", ctypes.c_void_p),
            ("CaptureTime", wintypes.FILETIME), ("Flags", wintypes.DWORD),
            ("SuspendCount", wintypes.WORD), ("SizeOfContextRecord", wintypes.WORD),
            ("ContextRecord", ctypes.c_void_p),
        ]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.PssCaptureSnapshot.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.HANDLE)]
    api.PssWalkMarkerCreate.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.HANDLE)]
    api.PssWalkSnapshot.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.HANDLE,
                                   ctypes.c_void_p, wintypes.DWORD]
    api.PssWalkMarkerFree.argtypes = [wintypes.HANDLE]
    api.PssFreeSnapshot.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    for name in ("PssCaptureSnapshot", "PssWalkMarkerCreate", "PssWalkSnapshot",
                 "PssWalkMarkerFree", "PssFreeSnapshot"):
        getattr(api, name).restype = wintypes.DWORD
    api.GetCurrentProcess.argtypes = []
    api.GetCurrentProcess.restype = wintypes.HANDLE

    snapshot, walk = wintypes.HANDLE(), wintypes.HANDLE()
    # PSS_CAPTURE_THREADS only: no memory, context capture, clone, suspend or resume.
    error = api.PssCaptureSnapshot(int(proc._handle), 0x80, 0, ctypes.byref(snapshot))
    if error:
        raise ctypes.WinError(error)
    try:
        error = api.PssWalkMarkerCreate(None, ctypes.byref(walk))
        if error:
            raise ctypes.WinError(error)
        counts = []
        while True:
            entry = ThreadEntry()
            error = api.PssWalkSnapshot(snapshot, 3, walk, ctypes.byref(entry), ctypes.sizeof(entry))
            if error == 259:  # ERROR_NO_MORE_ITEMS
                return counts
            if error:
                raise ctypes.WinError(error)
            if entry.ProcessId != proc.pid:
                raise ValueError("snapshot returned a thread outside the spawned child")
            counts.append({"pid": entry.ProcessId, "tid": entry.ThreadId,
                           "suspend_count": entry.SuspendCount, "exit_status": entry.ExitStatus})
    finally:
        # The snapshot is held by this process, even though its subject is the child.
        if walk.value:
            error = api.PssWalkMarkerFree(walk)
            if error:
                print("PssWalkMarkerFree diagnostic error:", error)
        error = api.PssFreeSnapshot(api.GetCurrentProcess(), snapshot)
        if error:
            print("PssFreeSnapshot diagnostic error:", error)
