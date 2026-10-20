"""面向客户端的安全错误；第三方异常正文不得直接暴露。"""

import traceback
from pathlib import Path


def error_location(exc: Exception) -> str:
    """仅记录文件名与行号，既能定位代码又不泄露堆栈中的路径和局部变量。"""
    return ",".join(f"{Path(frame.filename).name}:{frame.lineno}" for frame in traceback.extract_tb(exc.__traceback__)[-4:])


class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable
