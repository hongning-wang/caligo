import sys

# Remote controllers need only Python 3.9+ and the standard library. TOML is
# parsed on the client; do not import the Python 3.11 client on remote hosts.
if len(sys.argv) > 1 and sys.argv[1].startswith("_remote"):
    from .remote import main
else:
    from .cli import main

main()
