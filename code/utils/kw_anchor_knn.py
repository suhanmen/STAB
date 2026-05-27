from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import numpy as np

_CODE_DIR = Path(__file__).resolve().parents[1]
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))

_BASE_DIR = Path(__file__).resolve().parents[2]
_CACHE_DIR = _BASE_DIR / "dataset" / "scenario_knn_cache"
_META_PATH = _CACHE_DIR / "kw_anchor_meta.json"
_META_TRAIN_PATH = _CACHE_DIR / "kw_anchor_meta_train.json"
_TEST_EMB_PATH = _CACHE_DIR / "problems_test_SFR.npz"
_VALID_EMB_PATH = _CACHE_DIR / "problems_valid_SFR.npz"
_TRAIN_EMB_PATH = _CACHE_DIR / "anchors_train_kw_all_SFR.npz"
_TRAIN_EMB_PATH_FALLBACK = _CACHE_DIR / "anchors_train_SFR_balanced.npz"
_EXCLUDED_PATH = _BASE_DIR / "dataset" / "excluded_problems.json"

_DEFAULT_K = 5
_DEFAULT_TOP_P = 2
_DEFAULT_EMBED_MODEL = "Salesforce/SFR-Embedding-2_R"

def _get_anchor_source() -> str:
    return os.environ.get("ANCHOR_SOURCE", "train")

class KWAnchorKNN:

    _instance: Optional["KWAnchorKNN"] = None

    def __init__(self):
        self._meta = None
        self._anchor_embeddings = None
        self._anchor_labels = None
        self._anchor_names = None
        self._anchor_kws = None
        self._anchor_global_idx = None
        self._kw_matched_local = None
        self._encoder = None
        self._catalog = None
        self._anchor_source = None

    @classmethod
    def instance(cls) -> "KWAnchorKNN":
        source = _get_anchor_source()
        if cls._instance is not None and cls._instance._anchor_source != source:
            cls._instance = None
        if cls._instance is None:
            cls._instance = cls()
            cls._instance._load(source)
        return cls._instance

    def _load(self, anchor_source: str = "test_valid_loo"):
        self._anchor_source = anchor_source
        from utils.algorithm_adversary_catalog import load_catalog
        self._catalog = load_catalog()

        if anchor_source == "train":
            self._load_train()
        else:
            self._load_test_valid_loo()

    def _load_train(self):
        if not _META_TRAIN_PATH.exists():
            raise FileNotFoundError(
                f"{_META_TRAIN_PATH} not found. "
                "Run `python code/utils/build_kw_anchor_meta.py --splits train`")
        self._meta = json.loads(_META_TRAIN_PATH.read_text(encoding="utf-8"))

        train_emb_path = _TRAIN_EMB_PATH if _TRAIN_EMB_PATH.exists() else _TRAIN_EMB_PATH_FALLBACK
        if not train_emb_path.exists():
            raise FileNotFoundError(
                f"{_TRAIN_EMB_PATH} not found. "
                "Run `python code/utils/build_train_anchors.py --kw_all`")
        train_npz = np.load(_TRAIN_EMB_PATH, allow_pickle=True)
        train_emb = train_npz["embeddings"]
        train_names = list(train_npz["names"])
        train_name_set = set(train_names)

        npz_name_to_row = {n: i for i, n in enumerate(train_names)}

        names: List[str] = []
        labels: List[set] = []
        kws: List[set] = []
        emb_rows: List[int] = []
        n_missing = 0
        for key in sorted(self._meta["problems"].keys(),
                          key=lambda k: int(k.split("/")[1])):
            p = self._meta["problems"][key]
            if not p["has_kw"]:
                continue
            name = p["name"]
            row = npz_name_to_row.get(name)
            if row is None:
                n_missing += 1
                continue
            names.append(name)
            labels.append(set(p["kw_scenarios"]))
            kws.append(set(p["kw_scenarios"]))
            emb_rows.append(row)

        self._anchor_embeddings = train_emb[emb_rows]
        self._anchor_labels = labels
        self._anchor_names = names
        self._anchor_kws = kws
        self._anchor_global_idx = emb_rows
        self._kw_matched_local = list(range(len(names)))
        self._name_to_local = {n: i for i, n in enumerate(names)}

        self._scen_to_anchors: Dict[str, List[int]] = {}
        for j in range(len(names)):
            for s in labels[j]:
                self._scen_to_anchors.setdefault(s, []).append(j)
        self._scen_list: List[str] = sorted(self._scen_to_anchors.keys())
        if self._scen_list:
            cent = np.stack([
                self._anchor_embeddings[self._scen_to_anchors[s]].mean(axis=0)
                for s in self._scen_list
            ])
            norms = np.linalg.norm(cent, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self._scen_centroids: np.ndarray = cent / norms
        else:
            self._scen_centroids = np.zeros((0, self._anchor_embeddings.shape[1]))

        print(f"[KWAnchorKNN] loaded (train anchors): "
              f"pool={len(names)} KW-matched "
              f"(missing embeddings={n_missing}), "
              f"scenarios={len(self._scen_list)}, "
              f"dim={self._anchor_embeddings.shape[1]}")

    def _load_test_valid_loo(self):
        if not _META_PATH.exists():
            raise FileNotFoundError(
                f"{_META_PATH} not found. Run `python code/utils/build_kw_anchor_meta.py` first")
        self._meta = json.loads(_META_PATH.read_text(encoding="utf-8"))

        if not _TEST_EMB_PATH.exists() or not _VALID_EMB_PATH.exists():
            raise FileNotFoundError(
                "SFR embedding cache not found. Generate via scripts like `code/utils/build_train_anchors.py`")
        test_npz = np.load(_TEST_EMB_PATH, allow_pickle=True)
        valid_npz = np.load(_VALID_EMB_PATH, allow_pickle=True)
        all_emb = np.vstack([test_npz["embeddings"], valid_npz["embeddings"]])

        excluded_keys: set = set()
        if _EXCLUDED_PATH.exists():
            excl = json.loads(_EXCLUDED_PATH.read_text(encoding="utf-8"))
            for split_name in ("test", "valid"):
                for item in excl.get(split_name, []):
                    excluded_keys.add(f"{split_name}/{item['index']}")

        names: List[str] = []
        labels: List[set] = []
        kws: List[set] = []
        in_eval_set: List[bool] = []
        global_idx: List[int] = []
        test_n = len(test_npz["embeddings"])
        for key in sorted(self._meta["problems"].keys(),
                          key=lambda k: (k.split("/")[0], int(k.split("/")[1]))):
            p = self._meta["problems"][key]
            split, idx = p["split"], p["idx"]
            row = idx if split == "test" else (test_n + idx)
            global_idx.append(row)
            names.append(p["name"])
            labels.append(set(p["kw_scenarios"]))
            kws.append(set(p["kw_scenarios"]))
            in_eval_set.append(key not in excluded_keys)

        kw_matched_local = [
            i for i in range(len(names))
            if in_eval_set[i] and kws[i]
        ]

        self._anchor_embeddings = all_emb[global_idx]
        self._anchor_labels = labels
        self._anchor_names = names
        self._anchor_kws = kws
        self._anchor_global_idx = global_idx
        self._kw_matched_local = kw_matched_local
        self._name_to_local = {n: i for i, n in enumerate(self._anchor_names)}

        self._scen_to_anchors: Dict[str, List[int]] = {}
        for j in kw_matched_local:
            for s in labels[j]:
                self._scen_to_anchors.setdefault(s, []).append(j)
        self._scen_list: List[str] = sorted(self._scen_to_anchors.keys())
        if self._scen_list:
            cent = np.stack([
                self._anchor_embeddings[self._scen_to_anchors[s]].mean(axis=0)
                for s in self._scen_list
            ])
            norms = np.linalg.norm(cent, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self._scen_centroids: np.ndarray = cent / norms
        else:
            self._scen_centroids = np.zeros((0, self._anchor_embeddings.shape[1]))

        n_eval = sum(in_eval_set)
        print(f"[KWAnchorKNN] loaded (test_valid_loo): total={len(names)} (eval-set {n_eval}), "
              f"anchor pool={len(kw_matched_local)} "
              f"(KW-matched ∩ eval-set; labels=KW-detected scenarios), "
              f"scenarios={len(self._scen_list)}, dim={all_emb.shape[1]}")

    def keyword_match(self, problem_desc: str) -> List[str]:
        text = (problem_desc or "").lower()
        matched = []
        for scen_name, scen in self._catalog["scenarios"].items():
            det = scen.get("detection", {}) or {}
            sk = [k.lower() for k in det.get("strong_keywords", [])]
            if any(k in text for k in sk):
                matched.append(scen_name)
        return matched

    def _encode(self, problem_desc: str) -> np.ndarray:
        if self._encoder is None:
            from sentence_transformers import SentenceTransformer
            print(f"[KWAnchorKNN] loading encoder {_DEFAULT_EMBED_MODEL} (one-time)")
            self._encoder = SentenceTransformer(
                _DEFAULT_EMBED_MODEL,
                trust_remote_code=True,
                cache_folder=None,
            )
        emb = self._encoder.encode(problem_desc, normalize_embeddings=True)
        return np.asarray(emb, dtype=np.float32)

    def _knn_fallback(self, query_emb: np.ndarray, exclude_local_idx: Optional[int],
                       top_p: int = _DEFAULT_TOP_P) -> List[str]:
        if not self._scen_list:
            return []
        if exclude_local_idx is None:
            sims = self._scen_centroids @ query_emb
        else:
            sims = self._scen_centroids @ query_emb
            for k, s in enumerate(self._scen_list):
                if exclude_local_idx not in self._scen_to_anchors[s]:
                    continue
                remaining = [j for j in self._scen_to_anchors[s]
                             if j != exclude_local_idx]
                if not remaining:
                    sims[k] = -1.0
                    continue
                cent = self._anchor_embeddings[remaining].mean(axis=0)
                n = np.linalg.norm(cent)
                if n > 0:
                    cent = cent / n
                sims[k] = float(cent @ query_emb)
        p_eff = min(top_p, len(self._scen_list))
        if p_eff <= 0:
            return []
        order = np.argsort(-sims)[:p_eff]
        return [self._scen_list[k] for k in order]

    def detect_scenarios(self, problem_name: str, problem_desc: str,
                         top_p: int = _DEFAULT_TOP_P) -> List[str]:
        kw_matches = self.keyword_match(problem_desc)
        if kw_matches:
            return list(kw_matches)

        if self._anchor_source == "train":
            query_emb = self._encode(problem_desc)
            exclude = None
        else:
            local_idx = self._name_to_local.get(problem_name)
            if local_idx is not None:
                query_emb = self._anchor_embeddings[local_idx]
                exclude = local_idx
            else:
                query_emb = self._encode(problem_desc)
                exclude = None
        return self._knn_fallback(query_emb, exclude, top_p=top_p)

def detect_scenarios(problem_name: str, problem_desc: str,
                     top_p: int = _DEFAULT_TOP_P) -> List[str]:
    return KWAnchorKNN.instance().detect_scenarios(
        problem_name=problem_name, problem_desc=problem_desc,
        top_p=top_p,
    )

if __name__ == "__main__":
    inst = KWAnchorKNN.instance()
    print("\n=== sanity test ===")
    result = inst.detect_scenarios("1566_F. Points Movement", "")
    print(f"1566_F (empty desc, KNN fallback): {result}")
    result = inst.detect_scenarios("1575_A. Another Sorting Problem", "Sort this array of integers")
    print(f"1575_A (sort in desc, KW match): {result}")
