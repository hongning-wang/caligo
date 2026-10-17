import sys

if sys.version_info < (3, 11):
    sys.exit("caligo: Python 3.11+ is required; running " + sys.version.split()[0])

# Remote controllers use only the standard library. TOML is parsed locally.
if len(sys.argv) > 1 and sys.argv[1].startswith("_remote"):
    from .remote import main
else:
    from .cli import main

main()
