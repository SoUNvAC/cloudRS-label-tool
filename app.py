"""Local-first image annotation web application.

The application intentionally uses only the Python standard library.  Start it
with one target CSV so a reviewer cannot accidentally browse or edit another
reviewer's delivery file through the web interface.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import mimetypes
import os
import shutil
import sys
import tempfile
import threading
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


REVIEWER_COLUMNS = ("tile_id", "label_set", "notes")
CONSENSUS_COLUMNS = ("tile_id", "agreed_label_set", "rule_or_counterexample")
CONSENSUS_LABEL_COLUMNS = ("agreed_label_set", "consensus_label_set")
SINGLE_FILE_MERGE_COLUMNS = (
    "tile_id",
    "reviewer_A_label_set",
    "reviewer_B_label_set",
    "final_label_set",
    "rationale",
)
MAX_NOTES_LENGTH = 10_000
PROJECT_DEFAULT_LABEL_SOURCE = "__project_default_label_set__"


class UserFacingError(ValueError):
    """An invalid user input or local data layout."""


@dataclass(frozen=True)
class LabelDefinition:
    label: str
    description: str
    multi_selectable: bool
    exclusive: bool
    allowed_with: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "description": self.description,
            "multi_selectable": self.multi_selectable,
            "exclusive": self.exclusive,
            "allowed_with": list(self.allowed_with),
        }


def truthy(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise UserFacingError(f"布尔配置值无效：{value!r}")


class AnnotationStore:
    def __init__(
        self,
        data_dir: Path,
        csv_name: str,
        labels_name: str,
        *,
        label_column: str = "label_set",
        notes_column: str = "notes",
        mode: str = "annotation",
        reviewer_role: str = "",
        labels_path: Path | None = None,
    ) -> None:
        self.data_dir = data_dir.resolve()
        self.csv_path = self._safe_local_file(csv_name, suffix=".csv")
        self.labels_path = labels_path.resolve() if labels_path is not None else self._safe_local_file(labels_name, suffix=".csv")
        self.label_column = label_column
        self.notes_column = notes_column
        self.mode = mode
        self.reviewer_role = reviewer_role
        self.panels_dir = (self.data_dir / "panels").resolve()
        self.backup_dir = self.data_dir / ".annotation_history"
        self.audit_path = self.backup_dir / "audit.jsonl"
        self._write_lock = threading.RLock()

        if not self.panels_dir.is_dir():
            raise UserFacingError(f"找不到图片目录：{self.panels_dir}")
        if self.labels_path.suffix.lower() != ".csv" or not self.labels_path.is_file():
            raise UserFacingError(f"找不到 label_set CSV：{self.labels_path}")
        self._validate_csv_layout(self.csv_path)
        self.read_labels()

    def _safe_local_file(self, name: str, suffix: str) -> Path:
        candidate = Path(name)
        if candidate.is_absolute() or candidate.name != name or candidate.suffix.lower() != suffix:
            raise UserFacingError(f"只接受数据目录根部的 {suffix} 文件名：{name!r}")
        result = (self.data_dir / candidate).resolve()
        if result.parent != self.data_dir or not result.is_file():
            raise UserFacingError(f"找不到文件：{candidate}")
        return result

    @staticmethod
    def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames:
                    raise UserFacingError(f"CSV 没有表头：{path.name}")
                fieldnames = list(reader.fieldnames)
                rows = [{key: value or "" for key, value in row.items()} for row in reader]
                return fieldnames, rows
        except UnicodeDecodeError as error:
            raise UserFacingError(f"CSV 必须为 UTF-8 编码：{path.name}") from error

    def _validate_csv_layout(self, path: Path) -> None:
        fieldnames, rows = self._read_csv(path)
        missing = {"tile_id", self.label_column, self.notes_column}.difference(fieldnames)
        if missing:
            raise UserFacingError(f"{path.name} 缺少必需列：{', '.join(sorted(missing))}")
        tile_ids = [row["tile_id"].strip() for row in rows]
        if not rows:
            raise UserFacingError(f"{path.name} 没有待标注记录")
        if not all(tile_ids):
            raise UserFacingError(f"{path.name} 存在空 tile_id")
        if len(tile_ids) != len(set(tile_ids)):
            raise UserFacingError(f"{path.name} 存在重复 tile_id")

    def read_labels(self) -> list[LabelDefinition]:
        fieldnames, rows = self._read_csv(self.labels_path)
        if "label" not in fieldnames:
            raise UserFacingError("label_set.csv 必须包含 label 列")
        definitions: list[LabelDefinition] = []
        seen: set[str] = set()
        for number, row in enumerate(rows, start=2):
            label = row.get("label", "").strip()
            if not label:
                continue
            if "|" in label:
                raise UserFacingError(f"label_set.csv 第 {number} 行标签不能包含 |")
            if label in seen:
                raise UserFacingError(f"label_set.csv 存在重复标签：{label}")
            allowed_with = tuple(
                item.strip()
                for item in row.get("allowed_with", "").split("|")
                if item.strip()
            )
            definitions.append(
                LabelDefinition(
                    label=label,
                    description=row.get("description", "").strip(),
                    multi_selectable=truthy(row.get("multi_selectable"), True),
                    exclusive=truthy(row.get("exclusive"), False),
                    allowed_with=allowed_with,
                )
            )
            seen.add(label)
        if not definitions:
            raise UserFacingError("label_set.csv 为空；请至少配置一个 label")
        known = {definition.label for definition in definitions}
        for definition in definitions:
            unknown = set(definition.allowed_with).difference(known)
            if unknown:
                raise UserFacingError(
                    f"标签 {definition.label} 的 allowed_with 含未知标签：{', '.join(sorted(unknown))}"
                )
            if definition.exclusive and (definition.multi_selectable or definition.allowed_with):
                raise UserFacingError(f"独占标签 {definition.label} 不应配置 multi_selectable 或 allowed_with")
        return definitions

    def _labels_by_name(self) -> dict[str, LabelDefinition]:
        return {definition.label: definition for definition in self.read_labels()}

    @staticmethod
    def _label_values(value: Any) -> list[str]:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise UserFacingError("labels 必须是字符串数组")
        cleaned = [item.strip() for item in value if item.strip()]
        if len(cleaned) != len(set(cleaned)):
            raise UserFacingError("不能重复选择同一个标签")
        if len(cleaned) > 2:
            raise UserFacingError("最多只能选择两个标签")
        return cleaned

    def normalize_labels(self, selected: Any) -> str:
        chosen = self._label_values(selected)
        definitions = self._labels_by_name()
        unknown = set(chosen).difference(definitions)
        if unknown:
            raise UserFacingError(f"存在未配置的标签：{', '.join(sorted(unknown))}")
        if len(chosen) == 2:
            first, second = (definitions[item] for item in chosen)
            if first.exclusive or second.exclusive or not first.multi_selectable or not second.multi_selectable:
                raise UserFacingError("独占或不支持复选的标签不能组成双标签")
            first_allows = not first.allowed_with or second.label in first.allowed_with
            second_allows = not second.allowed_with or first.label in second.allowed_with
            if not (first_allows and second_allows):
                raise UserFacingError(f"不允许的双标签组合：{first.label}|{second.label}")
        order = {label: index for index, label in enumerate(definitions)}
        return "|".join(sorted(chosen, key=order.__getitem__))

    def state(self) -> dict[str, Any]:
        _, rows = self._read_csv(self.csv_path)
        normalized_name = "".join(character for character in self.csv_path.stem.lower() if character.isalnum())
        reviewer_role = self.reviewer_role or (
            "A" if normalized_name.startswith("reviewera") else "B" if normalized_name.startswith("reviewerb") else ""
        )
        return {
            "mode": self.mode,
            "workspace_mode": "review",
            "reviewer_role": reviewer_role,
            "csv_name": self.csv_path.name,
            "tiles": [
                {
                    "tile_id": row["tile_id"].strip(),
                    "label_set": row.get(self.label_column, ""),
                    "notes": row.get(self.notes_column, ""),
                    "has_image": self.image_path(row["tile_id"].strip()) is not None,
                }
                for row in rows
            ],
            "labels": [definition.as_json() for definition in self.read_labels()],
        }

    def image_path(self, tile_id: str) -> Path | None:
        if not tile_id or Path(tile_id).name != tile_id:
            return None
        candidate = (self.panels_dir / f"{tile_id}.png").resolve()
        if candidate.parent != self.panels_dir or not candidate.is_file():
            return None
        return candidate

    def save(self, tile_id: Any, labels: Any, notes: Any, reason: Any) -> dict[str, Any]:
        if not isinstance(tile_id, str) or not tile_id.strip():
            raise UserFacingError("tile_id 无效")
        tile_id = tile_id.strip()
        normalized_labels = self.normalize_labels(labels)
        if not isinstance(notes, str):
            raise UserFacingError("notes 必须是文本")
        if len(notes) > MAX_NOTES_LENGTH:
            raise UserFacingError(f"notes 不能超过 {MAX_NOTES_LENGTH} 个字符")
        if not isinstance(reason, str):
            reason = "manual"
        reason = reason[:40]

        with self._write_lock:
            fieldnames, rows = self._read_csv(self.csv_path)
            self._validate_rows(fieldnames, rows)
            target = next((row for row in rows if row["tile_id"].strip() == tile_id), None)
            if target is None:
                raise UserFacingError(f"找不到 tile_id：{tile_id}")
            previous = {"label_set": target.get(self.label_column, ""), "notes": target.get(self.notes_column, "")}
            target[self.label_column] = normalized_labels
            target[self.notes_column] = notes
            changed = previous != {"label_set": normalized_labels, "notes": notes}
            if changed:
                self._backup_original()
            self._write_rows_atomically(fieldnames, rows)
            self._append_audit(tile_id, previous, target, reason, changed)

        return {
            "tile_id": tile_id,
            "label_set": normalized_labels,
            "notes": notes,
            "changed": changed,
            "saved_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def _validate_rows(self, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
        missing = {"tile_id", self.label_column, self.notes_column}.difference(fieldnames)
        if missing:
            raise UserFacingError(f"CSV 已被外部修改，缺少列：{', '.join(sorted(missing))}")
        if not rows:
            raise UserFacingError("CSV 已被外部修改为空文件")

    def _backup_original(self) -> None:
        self.backup_dir.mkdir(exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = self.backup_dir / f"{self.csv_path.stem}.{stamp}.csv"
        shutil.copy2(self.csv_path, backup)

    def _write_rows_atomically(self, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", dir=self.data_dir, prefix=".annotation-", suffix=".tmp", delete=False
            ) as temporary:
                temp_name = temporary.name
                writer = csv.DictWriter(temporary, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temp_name, self.csv_path)
            temp_name = None
        finally:
            if temp_name:
                Path(temp_name).unlink(missing_ok=True)

    def _append_audit(
        self,
        tile_id: str,
        previous: dict[str, str],
        target: dict[str, str],
        reason: str,
        changed: bool,
    ) -> None:
        self.backup_dir.mkdir(exist_ok=True)
        record = {
            "timestamp": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "csv": self.csv_path.name,
            "mode": self.mode,
            "tile_id": tile_id,
            "reason": reason,
            "changed": changed,
            "before": previous,
            "after": {
                "label_set": target.get(self.label_column, ""),
                "notes": target.get(self.notes_column, ""),
            },
        }
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


class MergeStore(AnnotationStore):
    """Read two reviewer files and write only the consensus delivery file."""

    def __init__(
        self,
        data_dir: Path,
        reviewer_a_name: str,
        reviewer_b_name: str,
        consensus_name: str,
        labels_name: str,
        labels_path: Path | None = None,
    ) -> None:
        data_path = data_dir.resolve()
        consensus_candidate = Path(consensus_name)
        if (
            consensus_candidate.is_absolute()
            or consensus_candidate.name != consensus_name
            or consensus_candidate.suffix.lower() != ".csv"
        ):
            raise UserFacingError(f"只接受数据目录根部的 .csv 文件名：{consensus_name!r}")
        consensus_path = (data_path / consensus_candidate).resolve()
        if consensus_path.parent != data_path or not consensus_path.is_file():
            raise UserFacingError(f"找不到文件：{consensus_candidate}")
        consensus_fields, _ = self._read_csv(consensus_path)
        label_column = next((column for column in CONSENSUS_LABEL_COLUMNS if column in consensus_fields), None)
        if label_column is None:
            expected = " 或 ".join(CONSENSUS_LABEL_COLUMNS)
            raise UserFacingError(f"{consensus_path.name} 缺少最终共识标签列：{expected}")
        super().__init__(
            data_dir,
            consensus_name,
            labels_name,
            label_column=label_column,
            notes_column="rule_or_counterexample",
            mode="merge",
            labels_path=labels_path,
        )
        self.reviewer_a_path = self._safe_local_file(reviewer_a_name, suffix=".csv")
        self.reviewer_b_path = self._safe_local_file(reviewer_b_name, suffix=".csv")
        if self.csv_path in {self.reviewer_a_path, self.reviewer_b_path}:
            raise UserFacingError("最终共识 CSV 不能与 reviewer A 或 B 使用同一个文件")
        if self.reviewer_a_path == self.reviewer_b_path:
            raise UserFacingError("reviewer A 与 reviewer B 必须是两个不同的 CSV 文件")
        self._validate_merge_layout()

    @staticmethod
    def _validate_reviewer_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
        fieldnames, rows = AnnotationStore._read_csv(path)
        missing = set(REVIEWER_COLUMNS).difference(fieldnames)
        if missing:
            raise UserFacingError(f"{path.name} 缺少 reviewer 列：{', '.join(sorted(missing))}")
        tile_ids = [row["tile_id"].strip() for row in rows]
        if not rows or not all(tile_ids):
            raise UserFacingError(f"{path.name} 缺少有效的 reviewer 记录")
        if len(tile_ids) != len(set(tile_ids)):
            raise UserFacingError(f"{path.name} 存在重复 tile_id")
        return fieldnames, rows

    @staticmethod
    def _tile_ids(rows: list[dict[str, str]]) -> list[str]:
        return [row["tile_id"].strip() for row in rows]

    def _merge_rows(self) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
        _, reviewer_a_rows = self._validate_reviewer_rows(self.reviewer_a_path)
        _, reviewer_b_rows = self._validate_reviewer_rows(self.reviewer_b_path)
        consensus_fields, consensus_rows = self._read_csv(self.csv_path)
        self._validate_rows(consensus_fields, consensus_rows)
        expected = self._tile_ids(reviewer_a_rows)
        if self._tile_ids(reviewer_b_rows) != expected:
            raise UserFacingError("reviewer A 与 reviewer B 的 tile_id 或记录顺序不一致，已拒绝合并")
        if self._tile_ids(consensus_rows) != expected:
            raise UserFacingError("calibration_consensus.csv 与 reviewer A 的 tile_id 或记录顺序不一致，已拒绝合并")
        return reviewer_a_rows, reviewer_b_rows, consensus_rows

    def _validate_merge_layout(self) -> None:
        self._merge_rows()

    def _agreement_label_set(self, reviewer_a_label_set: str, reviewer_b_label_set: str) -> str:
        """Return a validated, canonical shared label set, otherwise an empty string."""
        if not reviewer_a_label_set.strip() or not reviewer_b_label_set.strip():
            return ""
        try:
            reviewer_a = self.normalize_labels(reviewer_a_label_set.split("|"))
            reviewer_b = self.normalize_labels(reviewer_b_label_set.split("|"))
        except UserFacingError:
            # Historical or malformed reviewer values need a human decision rather
            # than being silently copied into the final delivery file.
            return ""
        return reviewer_a if reviewer_a and reviewer_a == reviewer_b else ""

    def state(self) -> dict[str, Any]:
        reviewer_a_rows, reviewer_b_rows, consensus_rows = self._merge_rows()
        tiles = []
        for reviewer_a, reviewer_b, consensus in zip(reviewer_a_rows, reviewer_b_rows, consensus_rows, strict=True):
            tile_id = consensus["tile_id"].strip()
            agreement = self._agreement_label_set(
                reviewer_a.get("label_set", ""), reviewer_b.get("label_set", "")
            )
            tiles.append(
                {
                    "tile_id": tile_id,
                    "label_set": consensus.get(self.label_column, ""),
                    "notes": consensus.get(self.notes_column, ""),
                    "has_image": self.image_path(tile_id) is not None,
                    "reviewer_a": {
                        "label_set": reviewer_a.get("label_set", ""),
                        "notes": reviewer_a.get("notes", ""),
                    },
                    "reviewer_b": {
                        "label_set": reviewer_b.get("label_set", ""),
                        "notes": reviewer_b.get("notes", ""),
                    },
                    "reviewer_agreement_label_set": agreement,
                }
            )
        return {
            "mode": self.mode,
            "workspace_mode": "consensus",
            "csv_name": self.csv_path.name,
            "reviewer_a_name": self.reviewer_a_path.name,
            "reviewer_b_name": self.reviewer_b_path.name,
            "tiles": tiles,
            "labels": [definition.as_json() for definition in self.read_labels()],
        }

    def save(self, tile_id: Any, labels: Any, notes: Any, reason: Any) -> dict[str, Any]:
        # Re-check sources before each write so an externally replaced reviewer file
        # cannot silently be merged into a mismatched consensus file.
        self._validate_merge_layout()
        return super().save(tile_id, labels, notes, reason)

    def auto_merge_agreements(self) -> dict[str, int]:
        """Copy safe A/B agreements into otherwise blank consensus rows in one transaction."""
        with self._write_lock:
            reviewer_a_rows, reviewer_b_rows, consensus_rows = self._merge_rows()
            consensus_fields, _ = self._read_csv(self.csv_path)
            matched = 0
            saved = 0
            already_final = 0
            manual = 0
            for reviewer_a, reviewer_b, consensus in zip(reviewer_a_rows, reviewer_b_rows, consensus_rows, strict=True):
                agreement = self._agreement_label_set(
                    reviewer_a.get("label_set", ""), reviewer_b.get("label_set", "")
                )
                if not agreement:
                    manual += 1
                    continue
                matched += 1
                if consensus.get(self.label_column, "").strip():
                    already_final += 1
                    continue
                consensus[self.label_column] = agreement
                saved += 1

            if saved:
                self._backup_original()
                self._write_rows_atomically(consensus_fields, consensus_rows)
                self._append_auto_merge_audit(matched, saved, already_final, manual)

        return {
            "matched": matched,
            "saved": saved,
            "already_final": already_final,
            "manual": manual,
        }

    def _append_auto_merge_audit(self, matched: int, saved: int, already_final: int, manual: int) -> None:
        self.backup_dir.mkdir(exist_ok=True)
        record = {
            "timestamp": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "csv": self.csv_path.name,
            "mode": self.mode,
            "reason": "auto_reviewer_agreement",
            "matched": matched,
            "saved": saved,
            "already_final": already_final,
            "manual": manual,
        }
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


class SingleFileMergeStore(AnnotationStore):
    """Merge adjudication where both reviewer results and final fields share one CSV."""

    def __init__(self, data_dir: Path, csv_name: str, labels_name: str, labels_path: Path | None = None) -> None:
        super().__init__(
            data_dir,
            csv_name,
            labels_name,
            label_column="final_label_set",
            notes_column="rationale",
            mode="merge",
            labels_path=labels_path,
        )
        self._validate_single_file_layout()

    def _single_file_rows(self) -> tuple[list[str], list[dict[str, str]]]:
        fieldnames, rows = self._read_csv(self.csv_path)
        missing = set(SINGLE_FILE_MERGE_COLUMNS).difference(fieldnames)
        if missing:
            raise UserFacingError(f"{self.csv_path.name} 缺少单文件合并列：{', '.join(sorted(missing))}")
        tile_ids = [row["tile_id"].strip() for row in rows]
        if not rows or not all(tile_ids):
            raise UserFacingError(f"{self.csv_path.name} 缺少有效的待裁决记录")
        if len(tile_ids) != len(set(tile_ids)):
            raise UserFacingError(f"{self.csv_path.name} 存在重复 tile_id")
        return fieldnames, rows

    def _validate_single_file_layout(self) -> None:
        self._single_file_rows()

    def _agreement_label_set(self, reviewer_a_label_set: str, reviewer_b_label_set: str) -> str:
        if not reviewer_a_label_set.strip() or not reviewer_b_label_set.strip():
            return ""
        try:
            reviewer_a = self.normalize_labels(reviewer_a_label_set.split("|"))
            reviewer_b = self.normalize_labels(reviewer_b_label_set.split("|"))
        except UserFacingError:
            return ""
        return reviewer_a if reviewer_a and reviewer_a == reviewer_b else ""

    def state(self) -> dict[str, Any]:
        _, rows = self._single_file_rows()
        tiles = []
        for row in rows:
            tile_id = row["tile_id"].strip()
            reviewer_a_label_set = row.get("reviewer_A_label_set", "")
            reviewer_b_label_set = row.get("reviewer_B_label_set", "")
            tiles.append(
                {
                    "tile_id": tile_id,
                    "label_set": row.get(self.label_column, ""),
                    "notes": row.get(self.notes_column, ""),
                    "has_image": self.image_path(tile_id) is not None,
                    "reviewer_a": {"label_set": reviewer_a_label_set, "notes": ""},
                    "reviewer_b": {"label_set": reviewer_b_label_set, "notes": ""},
                    "reviewer_agreement_label_set": self._agreement_label_set(
                        reviewer_a_label_set, reviewer_b_label_set
                    ),
                }
            )
        return {
            "mode": self.mode,
            "workspace_mode": "single_file_consensus",
            "merge_layout": "single_file",
            "csv_name": self.csv_path.name,
            "reviewer_a_name": "reviewer_A_label_set",
            "reviewer_b_name": "reviewer_B_label_set",
            "tiles": tiles,
            "labels": [definition.as_json() for definition in self.read_labels()],
        }

    def save(self, tile_id: Any, labels: Any, notes: Any, reason: Any) -> dict[str, Any]:
        self._validate_single_file_layout()
        return super().save(tile_id, labels, notes, reason)

    def auto_merge_agreements(self) -> dict[str, int]:
        with self._write_lock:
            fieldnames, rows = self._single_file_rows()
            matched = 0
            saved = 0
            already_final = 0
            manual = 0
            for row in rows:
                agreement = self._agreement_label_set(
                    row.get("reviewer_A_label_set", ""), row.get("reviewer_B_label_set", "")
                )
                if not agreement:
                    manual += 1
                    continue
                matched += 1
                if row.get(self.label_column, "").strip():
                    already_final += 1
                    continue
                row[self.label_column] = agreement
                saved += 1
            if saved:
                self._backup_original()
                self._write_rows_atomically(fieldnames, rows)
                self._append_auto_merge_audit(matched, saved, already_final, manual)
        return {
            "matched": matched,
            "saved": saved,
            "already_final": already_final,
            "manual": manual,
        }

    def _append_auto_merge_audit(self, matched: int, saved: int, already_final: int, manual: int) -> None:
        self.backup_dir.mkdir(exist_ok=True)
        record = {
            "timestamp": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "csv": self.csv_path.name,
            "mode": "single_file_merge",
            "reason": "auto_reviewer_agreement",
            "matched": matched,
            "saved": saved,
            "already_final": already_final,
            "manual": manual,
        }
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


class WorkspaceManager:
    """Keeps one locally selected phase workspace active for this web server."""

    def __init__(self, data_dir: Path, default_labels: str, fallback_labels_path: Path | None = None) -> None:
        self.data_dir = data_dir.resolve()
        self.default_labels = default_labels
        self.fallback_labels_path = (
            fallback_labels_path.resolve()
            if fallback_labels_path is not None
            else (Path(__file__).parent / Path(default_labels).name).resolve()
        )
        self._store: AnnotationStore | None = None
        self._lock = threading.RLock()
        if not self.data_dir.is_dir():
            raise UserFacingError(f"找不到 phase 数据目录：{self.data_dir}")

    def _safe_csv_path(self, name: Any) -> Path:
        if not isinstance(name, str) or not name.strip():
            raise UserFacingError("请选择一个 CSV 文件")
        candidate = Path(name)
        if candidate.is_absolute() or candidate.name != name or candidate.suffix.lower() != ".csv":
            raise UserFacingError(f"只能选择 phase 根目录内的 CSV 文件：{name!r}")
        result = (self.data_dir / candidate).resolve()
        if result.parent != self.data_dir or not result.is_file():
            raise UserFacingError(f"找不到 CSV 文件：{candidate}")
        return result

    def _catalog_entry(self, path: Path) -> dict[str, Any]:
        try:
            fieldnames, rows = AnnotationStore._read_csv(path)
            fields = set(fieldnames)
            if "label" in fields:
                kind = "label_config"
            elif set(SINGLE_FILE_MERGE_COLUMNS).issubset(fields):
                kind = "single_file_merge"
            elif set(CONSENSUS_COLUMNS).issubset(fields):
                kind = "consensus_output"
            elif set(REVIEWER_COLUMNS).issubset(fields):
                kind = "reviewer"
            else:
                kind = "other"
            return {"name": path.name, "kind": kind, "rows": len(rows), "columns": fieldnames}
        except UserFacingError as error:
            return {"name": path.name, "kind": "invalid", "rows": 0, "columns": [], "error": str(error)}

    def navigation(self) -> dict[str, Any]:
        entries = [self._catalog_entry(path) for path in sorted(self.data_dir.glob("*.csv"), key=lambda item: item.name.lower())]
        label_files = [entry["name"] for entry in entries if entry["kind"] == "label_config"]
        if label_files:
            default_labels = self.default_labels if self.default_labels in label_files else label_files[0]
            label_sources = [
                {"name": name, "display_name": name, "path": str((self.data_dir / name).resolve()), "is_default": False}
                for name in label_files
            ]
        elif self.fallback_labels_path.is_file():
            default_labels = PROJECT_DEFAULT_LABEL_SOURCE
            label_sources = [{
                "name": PROJECT_DEFAULT_LABEL_SOURCE,
                "display_name": f"默认 {self.fallback_labels_path.name}（项目预设）",
                "path": str(self.fallback_labels_path),
                "is_default": True,
            }]
        else:
            default_labels = ""
            label_sources = []
        panels_dir = self.data_dir / "panels"
        panel_count = len(list(panels_dir.glob("*.png"))) if panels_dir.is_dir() else 0
        return {
            "data_dir": str(self.data_dir),
            "csv_files": entries,
            "label_files": label_files,
            "label_sources": label_sources,
            "default_labels": default_labels,
            "panel_count": panel_count,
            "active": self._store is not None,
        }

    def _labels_path(self, labels_name: Any) -> tuple[Path, bool]:
        if labels_name == PROJECT_DEFAULT_LABEL_SOURCE:
            if not self.fallback_labels_path.is_file():
                raise UserFacingError("目标目录没有 label_set，项目预设 label_set.csv 也不存在")
            return self.fallback_labels_path, True
        return self._safe_csv_path(labels_name), False

    def label_preview(self, labels_name: Any) -> dict[str, Any]:
        path, is_default = self._labels_path(labels_name)
        fieldnames, rows = AnnotationStore._read_csv(path)
        if "label" not in fieldnames:
            raise UserFacingError(f"{path.name} 不是 label_set 配置文件")
        preview = [
            {
                "label": row.get("label", "").strip(),
                "description": row.get("description", "").strip(),
                "multi_selectable": row.get("multi_selectable", ""),
                "exclusive": row.get("exclusive", ""),
            }
            for row in rows
            if row.get("label", "").strip()
        ]
        return {"name": path.name, "path": str(path), "is_default": is_default, "labels": preview}

    def activate(self, payload: dict[str, Any]) -> dict[str, Any]:
        mode = payload.get("mode")
        labels = payload.get("labels", self.default_labels)
        labels_path, _ = self._labels_path(labels)
        with self._lock:
            if mode == "review":
                reviewer_role = payload.get("reviewer_role", "")
                if reviewer_role not in {"", "A", "B"}:
                    raise UserFacingError("独立评审人只能选择 A 或 B")
                store: AnnotationStore = AnnotationStore(
                    self.data_dir,
                    payload.get("csv"),
                    labels,
                    reviewer_role=reviewer_role,
                    labels_path=labels_path,
                )
            elif mode == "consensus":
                store = MergeStore(
                    self.data_dir,
                    payload.get("reviewer_a"),
                    payload.get("reviewer_b"),
                    payload.get("consensus"),
                    labels,
                    labels_path=labels_path,
                )
            elif mode == "single_file_consensus":
                store = SingleFileMergeStore(self.data_dir, payload.get("csv"), labels, labels_path=labels_path)
            else:
                raise UserFacingError("请选择独立评审、共识裁决或单文件共识裁决模式")
            self._store = store
            return store.state()

    def current_store(self) -> AnnotationStore:
        with self._lock:
            if self._store is None:
                raise UserFacingError("尚未选择工作模式和 CSV 文件")
            return self._store


class AppHandler(BaseHTTPRequestHandler):
    workspace_manager: WorkspaceManager
    static_dir: Path

    server_version = "CloudRSLabelTool/0.1"

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/" or path == "/index.html":
                self._serve_file(self.static_dir / "index.html", "text/html; charset=utf-8")
            elif path == "/workspace":
                self._serve_file(self.static_dir / "workspace.html", "text/html; charset=utf-8")
            elif path == "/api/navigation":
                self._send_json(HTTPStatus.OK, self.workspace_manager.navigation())
            elif path == "/api/labels-preview":
                query = parse_qs(urlparse(self.path).query)
                self._send_json(HTTPStatus.OK, self.workspace_manager.label_preview(query.get("labels", [""])[0]))
            elif path == "/api/state":
                self._send_json(HTTPStatus.OK, self.workspace_manager.current_store().state())
            elif path == "/api/labels":
                store = self.workspace_manager.current_store()
                self._send_json(HTTPStatus.OK, {"labels": [item.as_json() for item in store.read_labels()]})
            elif path.startswith("/api/image/"):
                tile_id = path.removeprefix("/api/image/")
                image = self.workspace_manager.current_store().image_path(tile_id)
                if image is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "找不到对应 PNG 图片"})
                else:
                    self._serve_file(image, "image/png")
            elif path.startswith("/static/"):
                relative = Path(path.removeprefix("/static/"))
                candidate = (self.static_dir / relative).resolve()
                if candidate.parent != self.static_dir or not candidate.is_file():
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "静态文件不存在"})
                else:
                    content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
                    if content_type.startswith("text/") or candidate.suffix == ".js":
                        content_type += "; charset=utf-8"
                    self._serve_file(candidate, content_type)
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
        except UserFacingError as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:  # pragma: no cover - defensive HTTP boundary
            print(f"Unexpected request error: {error}", file=sys.stderr)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "服务器发生意外错误，请查看终端输出"})

    def do_POST(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        if path not in {"/api/workspace", "/api/save", "/api/auto-merge-agreements"}:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > 100_000:
                raise UserFacingError("请求内容长度无效")
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise UserFacingError("请求必须是 JSON 对象")
            if path == "/api/workspace":
                result = self.workspace_manager.activate(payload)
            else:
                store = self.workspace_manager.current_store()
                if path == "/api/auto-merge-agreements":
                    if not isinstance(store, (MergeStore, SingleFileMergeStore)):
                        raise UserFacingError("只有合并模式可以自动写入 A/B 一致项")
                    result = store.auto_merge_agreements()
                else:
                    result = store.save(
                        payload.get("tile_id"), payload.get("labels"), payload.get("notes"), payload.get("reason", "manual")
                    )
            self._send_json(HTTPStatus.OK, result)
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "请求不是有效 JSON"})
        except UserFacingError as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:  # pragma: no cover - defensive HTTP boundary
            print(f"Unexpected save error: {error}", file=sys.stderr)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "保存失败，请查看终端输出"})

    def _serve_file(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="本地图片标注 Web 工具")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--csv", help="要编辑的单一交付 CSV，例如 reviewer_A_main.csv")
    mode.add_argument("--merge", action="store_true", help="开启双 reviewer 的共识合并模式")
    mode.add_argument("--merge-csv", help="开启单文件的 A/B 合并裁决模式")
    parser.add_argument("--reviewer-a", help="合并模式中 reviewer A 的 CSV")
    parser.add_argument("--reviewer-b", help="合并模式中 reviewer B 的 CSV")
    parser.add_argument("--consensus", help="合并模式写入的 calibration_consensus.csv")
    parser.add_argument("--labels", default="label_set.csv", help="标签配置 CSV（默认：label_set.csv）")
    parser.add_argument("--data-dir", default=Path(__file__).parent, type=Path, help="CSV 和 panels 目录（默认：程序目录）")
    parser.add_argument("--host", default="127.0.0.1", help="默认仅监听本机；不要改为公网地址")
    parser.add_argument("--port", default=8765, type=int, help="服务端口（默认：8765）")
    parser.add_argument("--open-browser", action="store_true", help="启动后打开浏览器")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("安全限制：本工具仅允许监听本机地址。", file=sys.stderr)
        return 2
    try:
        workspace_manager = WorkspaceManager(args.data_dir, args.labels)
        direct_workspace = False
        if args.merge:
            missing = [
                option
                for option, value in {
                    "--reviewer-a": args.reviewer_a,
                    "--reviewer-b": args.reviewer_b,
                    "--consensus": args.consensus,
                }.items()
                if not value
            ]
            if missing:
                raise UserFacingError(f"合并模式缺少参数：{', '.join(missing)}")
            workspace_manager.activate(
                {
                    "mode": "consensus",
                    "reviewer_a": args.reviewer_a,
                    "reviewer_b": args.reviewer_b,
                    "consensus": args.consensus,
                    "labels": args.labels,
                }
            )
            direct_workspace = True
        elif args.merge_csv:
            workspace_manager.activate({"mode": "single_file_consensus", "csv": args.merge_csv, "labels": args.labels})
            direct_workspace = True
        elif args.csv:
            workspace_manager.activate({"mode": "review", "csv": args.csv, "labels": args.labels})
            direct_workspace = True
    except UserFacingError as error:
        print(f"启动失败：{error}", file=sys.stderr)
        return 2

    AppHandler.workspace_manager = workspace_manager
    AppHandler.static_dir = (Path(__file__).parent / "static").resolve()
    if not AppHandler.static_dir.is_dir():
        print("启动失败：找不到 static 目录。", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    url = f"http://{args.host}:{args.port}{'/workspace' if direct_workspace else '/'}"
    if direct_workspace:
        store = workspace_manager.current_store()
        if isinstance(store, MergeStore):
            print(f"合并评审：{store.reviewer_a_path.name} + {store.reviewer_b_path.name}")
            print(f"最终写入：{store.csv_path.name}")
        elif isinstance(store, SingleFileMergeStore):
            print(f"单文件合并评审：{store.csv_path.name}")
        else:
            print(f"正在编辑：{store.csv_path.name}")
    else:
        print(f"导航模式：请选择 phase 工作区中的 CSV 文件（{workspace_manager.data_dir}）")
    print(f"打开浏览器：{url}")
    print("按 Ctrl+C 停止服务。")
    if args.open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
