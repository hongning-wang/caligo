import json
import os
from pathlib import Path

outputs = Path(os.environ["CALIGO_OUTPUTS"])
predictions = json.loads((outputs / "predictions.json").read_text())
passed = predictions == [2, 4, 6]
(Path(os.environ["CALIGO_EVALUATION"]) / "metrics.json").write_text(json.dumps({"passed": passed}))
print("Fixed evaluation:", "passed" if passed else "failed", flush=True)
raise SystemExit(0 if passed else 1)
