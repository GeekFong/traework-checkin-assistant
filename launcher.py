# -*- coding: utf-8 -*-
"""PyInstaller 打包入口（等价于 python -m trae_checkin）。"""

import sys

from trae_checkin.cli import main

if __name__ == "__main__":
    sys.exit(main())
