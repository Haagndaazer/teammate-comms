"""Find and close the agent session that owns this MCP server. Windows only.

Used by ``teammate_shutdown``: the target's own server walks its ancestors to the harness
process and terminates that process tree. Every lookup is injectable so the selection
logic is tested with fake process tables.
"""
import os
import sys

LAUNCHER_NAMES = ("uv.exe", "uvx.exe", "py.exe")
LAUNCHER_PREFIXES = ("python",)
MAX_DEPTH = 20
SUPPORTED = os.name == "nt"

_TH32CS_SNAPPROCESS = 0x00000002
_QUERY_LIMITED = 0x1000
_TERMINATE = 0x0001
_INVALID_HANDLE = -1


def _kernel32():
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k.CloseHandle.argtypes = (wintypes.HANDLE,)
    k.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    k.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    k.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    return k


def snapshot():
    """``{pid: (parent_pid, exe_name_lower)}`` for every process, or {} if unavailable."""
    if not SUPPORTED:
        return {}
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    k = _kernel32()
    k.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
    k.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
    snap = k.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wintypes.HANDLE(_INVALID_HANDLE).value:
        return {}
    table = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        ok = k.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            table[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID),
                                               entry.szExeFile.lower())
            ok = k.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k.CloseHandle(snap)
    return table


def created(pid):
    """Process creation time as an integer, or None if it cannot be read."""
    if not SUPPORTED:
        return None
    import ctypes
    from ctypes import wintypes
    k = _kernel32()
    handle = k.OpenProcess(_QUERY_LIMITED, False, pid)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        k.CloseHandle(handle)


def image_path(pid):
    """Full executable path, lowercased, or "" if it cannot be read."""
    if not SUPPORTED:
        return ""
    import ctypes
    from ctypes import wintypes
    k = _kernel32()
    handle = k.OpenProcess(_QUERY_LIMITED, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if not k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value.lower()
    finally:
        k.CloseHandle(handle)


def terminate(pid, exit_code=1, born=None):
    """End ``pid``. With ``born``, only if the process holding that pid was created then,
    checked on the same handle that ends it, so a reused pid is never ended."""
    if not SUPPORTED:
        return False
    import ctypes
    from ctypes import wintypes
    k = _kernel32()
    handle = k.OpenProcess(_TERMINATE | _QUERY_LIMITED, False, pid)
    if not handle:
        return False
    try:
        if born is not None:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not k.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return False
            if ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) != born:
                return False
        return bool(k.TerminateProcess(handle, exit_code))
    finally:
        k.CloseHandle(handle)


def can_terminate(pid):
    if not SUPPORTED:
        return False
    k = _kernel32()
    handle = k.OpenProcess(_TERMINATE, False, pid)
    if not handle:
        return False
    k.CloseHandle(handle)
    return True


def _is_launcher(name):
    return name in LAUNCHER_NAMES or name.startswith(LAUNCHER_PREFIXES)


def _older(parent, child, created_fn):
    a, b = created_fn(parent), created_fn(child)
    return a is not None and b is not None and a <= b


def find_session(table, pid, names, path_excludes=(), created_fn=created, path_fn=image_path):
    """The harness process that started this server, or None when it cannot be told safely.

    Walks up past the launchers that sit between a harness and its MCP server; the first
    other ancestor must carry one of the harness's own process names. Anything else, or a
    parent that is not older than its child (a reused pid), returns None.
    """
    cur = pid
    for _ in range(MAX_DEPTH):
        if cur not in table:
            return None
        parent = table[cur][0]
        if parent not in table or parent == cur or not _older(parent, cur, created_fn):
            return None
        name = table[parent][1]
        if _is_launcher(name):
            cur = parent
            continue
        if name not in names:
            return None
        path = path_fn(parent)
        if any(part in path for part in path_excludes):
            return None
        return parent
    return None


def descendants(table, pid, created_fn=created, protect=()):
    """Every process below ``pid``, deepest first. A protected pid and all below it are left out."""
    children = {}
    for child, (parent, _name) in table.items():
        children.setdefault(parent, []).append(child)
    out, stack, seen = [], [pid], {pid}
    while stack:
        cur = stack.pop()
        for child in children.get(cur, []):
            if child in seen or child in protect or not _older(cur, child, created_fn):
                continue
            seen.add(child)
            out.append(child)
            stack.append(child)
    return out[::-1]


def close_session(session_pid, self_pid=None, table=None, kill=None, created_fn=created,
                  protect=(), can=can_terminate):
    """Terminate the session's process tree, this process last. Returns the pids ended; the
    session was closed only if ``session_pid`` is among them. Nothing is touched when the
    session process cannot be terminated.

    ``protect`` names processes that must outlive the session although they were started
    from inside it, such as this machine's relay. The session process itself is ended with
    exit code 0: a terminal tab whose own program was that process then closes by itself,
    and no other process is touched to make it so. Each process is ended only if it is
    still the one seen when the tree was read.
    """
    self_pid = os.getpid() if self_pid is None else self_pid
    if session_pid in protect or not can(session_pid):
        return []
    table = snapshot() if table is None else table
    below = [p for p in descendants(table, session_pid, created_fn, protect) if p != self_pid]
    own = kill is None
    if own:
        born = {p: created_fn(p) for p in below + [session_pid]}
        if born[session_pid] is None:
            return []

        def kill(pid, exit_code=1):
            return born.get(pid) is not None and terminate(pid, exit_code, born[pid])
    done = [p for p in below if kill(p)]
    if not kill(session_pid, 0):
        return done
    done.append(session_pid)
    if self_pid != session_pid:
        (terminate if own else kill)(self_pid)
    return done


def own_session(names, path_excludes=()):
    """This server's harness process, or None."""
    if not SUPPORTED:
        return None
    return find_session(snapshot(), os.getpid(), names, path_excludes)


if __name__ == "__main__":
    from . import harness
    h = harness.current()
    print(own_session(harness.PROCESS_NAMES[h.name], harness.PROCESS_PATH_EXCLUDES),
          file=sys.stdout)
