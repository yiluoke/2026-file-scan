"""python -m file_scan でも、インストール済みCLIと同じ処理を起動する。"""

from file_scan.cli import main

raise SystemExit(main())
