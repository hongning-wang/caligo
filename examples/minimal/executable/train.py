import json
import os
from pathlib import Path
import sys

workspace = Path(os.environ["CALIGO_WORKSPACE"])
sys.path.insert(0, str(workspace / "src"))
from model import predict

values = json.loads((workspace / "data/values.json").read_text())
predictions = [predict(x) for x in values]
outputs = Path(os.environ["CALIGO_OUTPUTS"])
(outputs / "predictions.json").write_text(json.dumps(predictions))
print("Predictions:", predictions, flush=True)
