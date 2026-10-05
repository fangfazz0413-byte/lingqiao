"""Windows 凭据管理器：额度密钥在 Windows 上存这里（相当于 macOS 的钥匙串）。

系统按当前用户加密保存，别的用户读不到；不经过文件，也不进子进程。只在 Windows 上导入。
"""
import ctypes
from ctypes import wintypes

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


class CREDENTIAL(ctypes.Structure):
    _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD), ('TargetName', wintypes.LPWSTR),
                ('Comment', wintypes.LPWSTR), ('LastWritten', wintypes.FILETIME),
                ('CredentialBlobSize', wintypes.DWORD), ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)),
                ('Persist', wintypes.DWORD), ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
                ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]


_advapi = ctypes.WinDLL('advapi32', use_last_error=True)
_advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
_advapi.CredReadW.restype = wintypes.BOOL
_advapi.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
_advapi.CredWriteW.restype = wintypes.BOOL
_advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
_advapi.CredDeleteW.restype = wintypes.BOOL
_advapi.CredFree.argtypes = [ctypes.c_void_p]
_advapi.CredFree.restype = None


def read(target):
    """返回保存的字节；没存过返回 None。"""
    pointer = ctypes.POINTER(CREDENTIAL)()
    if not _advapi.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        error = ctypes.get_last_error()
        if error == ERROR_NOT_FOUND:
            return None
        raise ctypes.WinError(error)
    try:
        cred = pointer.contents
        return ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
    finally:
        _advapi.CredFree(pointer)


def write(target, user, data):
    """新建或覆盖。"""
    blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    cred = CREDENTIAL(Type=CRED_TYPE_GENERIC, TargetName=target, CredentialBlobSize=len(data),
                      CredentialBlob=ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte)),
                      Persist=CRED_PERSIST_LOCAL_MACHINE, UserName=user)
    if not _advapi.CredWriteW(ctypes.byref(cred), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def delete(target):
    """删掉；本来就没有也算成功。"""
    if not _advapi.CredDeleteW(target, CRED_TYPE_GENERIC, 0):
        error = ctypes.get_last_error()
        if error != ERROR_NOT_FOUND:
            raise ctypes.WinError(error)
