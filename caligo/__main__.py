import sys

if sys.version_info < (3, 11):
    sys.exit("caligo: Python 3.11+ is required; running " + sys.version.split()[0])

if len(sys.argv) > 1 and sys.argv[1] == "_service":
    from .service import main

    main(sys.argv[2], sys.argv[3])
    sys.exit(0)
if len(sys.argv) > 1 and sys.argv[1] == "_agent-worker":
    import asyncio
    from .agents import worker

    sys.exit(asyncio.run(worker(sys.argv[2], sys.argv[3] or None, sys.argv[4] or None)))

# Remote controllers use only the standard library. TOML is parsed locally.
if len(sys.argv) > 1 and sys.argv[1].startswith("_remote"):
    from .remote import main
else:
    from .cli import main

main()
