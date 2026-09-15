# -*- coding: utf-8 -*-
"""支持 ``python -m trae_checkin`` 启动，同时作为 PyInstaller 打包入口。"""

import sys

from trae_checkin.cli import main

if __name__ == "__main__":
    sys.exit(main())
