"""Controller operations shared by local calls and the SSH entry point."""


def dispatch(controller, action, values, worker):
    """Return NotImplemented for operations owned by the calling entry point."""
    if action == "service-start":
        from .service import start

        return start(controller, values["state"])
    if action == "service-request":
        from .service import request

        return request(
            controller, values["method"], values["path"], values.get("values")
        )
    if action == "service-run":
        from .service import run_record

        return run_record(controller, values["run"])
    if action == "migrate-human":
        return controller.migrate_human(values["environment"])
    if action == "bootstrap":
        return controller.bootstrap(values["archive"], values.get("development", False))
    if action == "create":
        return controller.create(
            values["mode"], values["name"], values.get("addons", [])
        )
    if action == "environment":
        return controller.environment(values["environment"])
    if action == "fork":
        return controller.fork(values["environment"], values["name"])
    if action == "delete":
        return controller.delete(values["environment"])
    if action == "snapshot":
        return controller.snapshot(values["environment"], values.get("script"))
    if action == "receive":
        return controller.receive(values["archive"], worker)
    if action == "submit-dev":
        return controller.submit_dev(
            values["environment"],
            values["argv"],
            values["origin"],
            worker,
        )
    if action == "tail":
        return controller.tail(values["run"], values.get("offset", 0))
    if action == "status":
        return controller.status(values["run"])
    if action == "cancel":
        return controller.cancel(values["run"])
    if action == "publish":
        controller.publish(values["run"])
        return controller.status(values["run"], refresh=False)
    if action == "logs":
        path = controller.run_dir(values["run"]) / "logs.txt"
        return {"logs": path.read_text(errors="replace") if path.exists() else ""}
    return NotImplemented
