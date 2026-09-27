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
from urllib.parse import unquote, urlparse


REQUIRED_COLUMNS = ("tile_id", "label_set", "notes")
MAX_NOTES_LENGTH = 10_000


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
    def __init__(self, data_dir: Path, csv_name: str, labels_name: str) -> None:
        self.data_dir = data_dir.resolve()
        self.csv_path = self._safe_local_file(csv_name, suffix=".csv")
        self.labels_path = self._safe_local_file(labels_name, suffix=".csv")
        self.panels_dir = (self.data_dir / "panels").resolve()
        self.backup_dir = self.data_dir / ".annotation_history"
        self.audit_path = self.backup_dir / "audit.jsonl"
        self._write_lock = threading.RLock()

        if not self.panels_dir.is_dir():
            raise UserFacingError(f"找不到图片目录：{self.panels_dir}")
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
        missing = set(REQUIRED_COLUMNS).difference(fieldnames)
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
        return {
            "csv_name": self.csv_path.name,
            "tiles": [
                {
                    "tile_id": row["tile_id"].strip(),
                    "label_set": row.get("label_set", ""),
                    "notes": row.get("notes", ""),
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
            previous = {"label_set": target.get("label_set", ""), "notes": target.get("notes", "")}
            target["label_set"] = normalized_labels
            target["notes"] = notes
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

    @staticmethod
    def _validate_rows(fieldnames: list[str], rows: list[dict[str, str]]) -> None:
        missing = set(REQUIRED_COLUMNS).difference(fieldnames)
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
            "tile_id": tile_id,
            "reason": reason,
            "changed": changed,
            "before": previous,
            "after": {"label_set": target.get("label_set", ""), "notes": target.get("notes", "")},
        }
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


class AppHandler(BaseHTTPRequestHandler):
    store: AnnotationStore
    static_dir: Path

    server_version = "CloudRSLabelTool/0.1"

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/" or path == "/index.html":
                self._serve_file(self.static_dir / "index.html", "text/html; charset=utf-8")
            elif path == "/api/state":
                self._send_json(HTTPStatus.OK, self.store.state())
            elif path == "/api/labels":
                self._send_json(HTTPStatus.OK, {"labels": [item.as_json() for item in self.store.read_labels()]})
            elif path.startswith("/api/image/"):
                tile_id = path.removeprefix("/api/image/")
                image = self.store.image_path(tile_id)
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
        if path != "/api/save":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > 100_000:
                raise UserFacingError("请求内容长度无效")
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise UserFacingError("请求必须是 JSON 对象")
            result = self.store.save(
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
    parser.add_argument("--csv", required=True, help="要编辑的单一交付 CSV，例如 reviewer_A_main.csv")
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
        store = AnnotationStore(args.data_dir, args.csv, args.labels)
    except UserFacingError as error:
        print(f"启动失败：{error}", file=sys.stderr)
        return 2

    AppHandler.store = store
    AppHandler.static_dir = (Path(__file__).parent / "static").resolve()
    if not AppHandler.static_dir.is_dir():
        print("启动失败：找不到 static 目录。", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"正在编辑：{store.csv_path.name}")
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
