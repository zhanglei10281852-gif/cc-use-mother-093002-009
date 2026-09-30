import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from consent_governance.contracts import ConsentVersion, DatasetSource

entity = ConsentVersion("E-DEMO", "跨校学习数据授权治理", 1)
record = DatasetSource("R-DEMO", entity.entity_id, "已登记")
print(json.dumps({"entity": entity.display_name, "revision": entity.revision, "record_state": record.category}, ensure_ascii=False))
