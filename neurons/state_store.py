"""Simple JSON-backed state store for validator runtime state.

Tracks watermark and last scores for EMA smoothing.
"""
from __future__ import annotations

import os
import json
import time
from typing import Optional, Dict


class StateStore:
    def __init__(self, base_dir: Optional[str] = None, filename: str = "state_store.json"):
        directory = base_dir or os.getenv("VALIDATOR_STATE_DIR") or os.getcwd()
        self.path = os.path.join(directory, filename)
        self._data = {
            "watermark_to_ts": None,
            "last_scores": {},
            "last_epoch_index": None,
            "last_weight_block": None,
            "last_saved_at": None,
        }
        self._load()

    def _load(self) -> None:
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    obj = json.load(f)
                if isinstance(obj, dict):
                    self._data.update(obj)
                    # Log successful recovery if bittensor is available
                    try:
                        import bittensor as bt
                        bt.logging.info(
                            f"State recovered: epoch={obj.get('last_epoch_index')}",
                            prefix="STATE"
                        )
                    except Exception:
                        pass
        except Exception as e:
            # Log the error instead of silently swallowing if bittensor is available
            try:
                import bittensor as bt
                bt.logging.warning(f"Could not load state: {e}", prefix="STATE")
            except Exception:
                pass

    def _save(self) -> None:
        try:
            # Update timestamp
            self._data["last_saved_at"] = time.time()
            
            # Create backup of existing state before overwriting
            if os.path.exists(self.path):
                backup_path = self.path + ".backup"
                try:
                    import shutil
                    shutil.copy2(self.path, backup_path)
                except Exception:
                    pass  # Backup is optional
            
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
        except Exception as e:
            # Log save failures if bittensor is available
            try:
                import bittensor as bt
                bt.logging.error(f"Failed to save state: {e}", prefix="STATE")
            except Exception:
                pass

    def get_watermark(self) -> Optional[str]:
        wm = self._data.get("watermark_to_ts")
        return str(wm) if wm else None

    def commit_window(self, to_ts: str, last_scores: Dict[str, float]) -> None:
        self._data["watermark_to_ts"] = to_ts
        self._data["last_scores"] = last_scores or {}
        self._save()

    def commit_epoch(self, epoch_index: int, to_ts: str, last_scores: Dict[str, float]) -> None:
        self._data["last_epoch_index"] = int(epoch_index)
        self._data["watermark_to_ts"] = to_ts
        self._data["last_scores"] = last_scores or {}
        self._data["last_saved_at"] = time.time()
        self._save()

    def get_last_epoch(self) -> Optional[int]:
        le = self._data.get("last_epoch_index")
        try:
            return int(le) if le is not None else None
        except Exception:
            return None

    def set_last_weight_block(self, block: int) -> None:
        self._data["last_weight_block"] = int(block)
        self._save()

    def get_last_weight_block(self) -> Optional[int]:
        lw = self._data.get("last_weight_block")
        try:
            return int(lw) if lw is not None else None
        except Exception:
            return None

    def get_last_scores(self) -> Dict[str, float]:
        ls = self._data.get("last_scores") or {}
        if not isinstance(ls, dict):
            return {}
        # coerce to float
        out: Dict[str, float] = {}
        for k, v in ls.items():
            try:
                out[str(k)] = float(v)
            except Exception:
                continue
        return out


