"""Allow `python -m hive` to work as `hive` CLI."""
from hive.cli.main import main
import sys
sys.exit(main())
