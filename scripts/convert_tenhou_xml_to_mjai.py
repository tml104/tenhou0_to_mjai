#!/usr/bin/env python3
"""Convert Tenhou XML paipu files into line-delimited mjai JSON logs."""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import os
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote


MJAI_SUFFIX = ".json"
PROGRESS_INTERVAL_SECONDS = 2.0
TENHOU_RED_TILE_IDS = {16: "5mr", 52: "5pr", 88: "5sr"}
DRAW_TAG_TO_ACTOR = {"T": 0, "U": 1, "V": 2, "W": 3}
DISCARD_TAG_TO_ACTOR = {"D": 0, "E": 1, "F": 2, "G": 3}
WIND_TILES = ("E", "S", "W", "N")
HONOR_TILES = ("E", "S", "W", "N", "P", "F", "C")


class ConversionError(RuntimeError):
    """Raised when a paipu cannot be converted safely."""


def tile_id_to_mjai(tile_id: int) -> str:
    """把天凤 0..135 的牌 ID 转成 mjai 牌字符串，兼容赤 5。"""
    if tile_id in TENHOU_RED_TILE_IDS:
        return TENHOU_RED_TILE_IDS[tile_id]

    tile_type = tile_id // 4
    if tile_type < 0 or tile_type >= 34:
        raise ConversionError(f"invalid tile id: {tile_id}")

    if tile_type < 9:
        return f"{tile_type + 1}m"
    if tile_type < 18:
        return f"{tile_type - 8}p"
    if tile_type < 27:
        return f"{tile_type - 17}s"
    return HONOR_TILES[tile_type - 27]


def decode_scores(ten_attr: str) -> list[int]:
    """把天凤 ten 属性里的百点制分数转换成 mjai 使用的实际点数。"""
    return [int(value) * 100 for value in ten_attr.split(",")]


def decode_tiles(csv_value: str) -> list[str]:
    """把逗号分隔的天凤牌 ID 列表转换成 mjai 牌字符串列表。"""
    if not csv_value:
        return []
    return [tile_id_to_mjai(int(value)) for value in csv_value.split(",")]


def parse_sc_deltas(sc_attr: str | None) -> list[int] | None:
    """解析 AGARI/RYUUKYOKU 的 sc 字段，提取四家的点数变化。"""
    if not sc_attr:
        return None
    values = [int(float(value)) for value in sc_attr.split(",")]
    if len(values) != 8:
        raise ConversionError(f"unexpected sc payload: {sc_attr}")
    return [values[index] * 100 for index in range(1, 8, 2)]


def relative_target(who: int, from_who: int) -> int:
    """把天凤副露编码中的相对方位转换成绝对玩家编号。"""
    return (who + from_who) % 4


def decode_chi(meld_code: int) -> tuple[str, int, int, list[str]]:
    """解码天凤顺子副露编码，返回吃牌类型、来源方位和牌组明细。"""
    from_who = meld_code & 0x3
    pattern = (meld_code >> 10) & 0x3F
    called_index = pattern % 3
    pattern //= 3
    start = pattern % 7
    suit = pattern // 7
    if suit >= 3:
        raise ConversionError(f"invalid chi suit in meld: {meld_code}")

    base_kind = suit * 9 + start
    copy_indexes = [
        (meld_code >> 3) & 0x3,
        (meld_code >> 5) & 0x3,
        (meld_code >> 7) & 0x3,
    ]
    tile_ids = [
        (base_kind + offset) * 4 + copy_indexes[offset]
        for offset in range(3)
    ]
    called_tile = tile_id_to_mjai(tile_ids[called_index])
    consumed = [
        tile_id_to_mjai(tile_ids[offset])
        for offset in range(3)
        if offset != called_index
    ]
    return "chi", from_who, called_index, [called_tile, *consumed]


def decode_pon_or_kakan(meld_code: int) -> tuple[str, int, int, list[str]]:
    """解码天凤碰/加杠编码，区分 pon 与 kakan 并恢复牌组。"""
    from_who = meld_code & 0x3
    pattern = (meld_code >> 9) & 0x7F
    called_index = pattern % 3
    tile_kind = pattern // 3
    if tile_kind >= 34:
        raise ConversionError(f"invalid pon/kakan tile kind in meld: {meld_code}")

    added_index = (meld_code >> 5) & 0x3
    tile_ids = [tile_kind * 4 + index for index in range(4)]

    if meld_code & 0x8:
        meld_tile_ids = [tile_id for idx, tile_id in enumerate(tile_ids) if idx != added_index]
        called_tile = tile_id_to_mjai(meld_tile_ids[called_index])
        consumed = [
            tile_id_to_mjai(tile_id)
            for idx, tile_id in enumerate(meld_tile_ids)
            if idx != called_index
        ]
        return "pon", from_who, called_index, [called_tile, *consumed]

    added_tile = tile_id_to_mjai(tile_ids[added_index])
    consumed = [
        tile_id_to_mjai(tile_id)
        for idx, tile_id in enumerate(tile_ids)
        if idx != added_index
    ]
    return "kakan", from_who, called_index, [added_tile, *consumed]


def decode_kan(meld_code: int) -> tuple[str, int, int, list[str]]:
    """解码天凤大明杠/暗杠编码，返回杠类型和对应牌组。"""
    from_who = meld_code & 0x3
    pattern = (meld_code >> 8) & 0xFF
    called_index = pattern % 4
    tile_kind = pattern // 4
    if tile_kind >= 34:
        raise ConversionError(f"invalid kan tile kind in meld: {meld_code}")

    tile_ids = [tile_kind * 4 + index for index in range(4)]
    tiles = [tile_id_to_mjai(tile_id) for tile_id in tile_ids]
    if from_who == 0:
        return "ankan", from_who, called_index, tiles

    called_tile = tiles[called_index]
    consumed = [tile for idx, tile in enumerate(tiles) if idx != called_index]
    return "daiminkan", from_who, called_index, [called_tile, *consumed]


def decode_meld(meld_code: int, who: int) -> dict[str, object]:
    """把天凤 N 标签中的 m 编码统一转换成 mjai 的副露/杠事件。"""
    if meld_code & 0x4:
        meld_type, from_who, _, payload = decode_chi(meld_code)
        return {
            "type": meld_type,
            "actor": who,
            "target": relative_target(who, from_who),
            "pai": payload[0],
            "consumed": payload[1:],
        }

    if meld_code & 0x18:
        meld_type, from_who, _, payload = decode_pon_or_kakan(meld_code)
        if meld_type == "pon":
            return {
                "type": "pon",
                "actor": who,
                "target": relative_target(who, from_who),
                "pai": payload[0],
                "consumed": payload[1:],
            }
        return {
            "type": "kakan",
            "actor": who,
            "pai": payload[0],
            "consumed": payload[1:],
        }

    if meld_code & 0x20:
        raise ConversionError("three-player kita meld is not supported")

    meld_type, from_who, _, payload = decode_kan(meld_code)
    if meld_type == "ankan":
        return {"type": "ankan", "actor": who, "consumed": payload}
    return {
        "type": "daiminkan",
        "actor": who,
        "target": relative_target(who, from_who),
        "pai": payload[0],
        "consumed": payload[1:],
    }


def parse_names(un_element: ET.Element | None) -> list[str]:
    """从 UN 标签中提取四家名字，并做 URL 解码。"""
    names = []
    for index in range(4):
        raw = "" if un_element is None else un_element.get(f"n{index}", "")
        names.append(unquote(raw))
    return names


def build_start_kyoku_event(init_element: ET.Element) -> dict[str, object]:
    """把 INIT 标签转换成 mjai 的 start_kyoku 事件。"""
    seed_values = [int(value) for value in init_element.attrib["seed"].split(",")]
    round_index = seed_values[0]
    bakaze = WIND_TILES[round_index // 4]
    kyoku = (round_index % 4) + 1
    tehais = [
        decode_tiles(init_element.attrib[f"hai{player}"])
        for player in range(4)
    ]
    return {
        "type": "start_kyoku",
        "bakaze": bakaze,
        "dora_marker": tile_id_to_mjai(seed_values[5]),
        "kyoku": kyoku,
        "honba": seed_values[1],
        "kyotaku": seed_values[2],
        "oya": int(init_element.attrib["oya"]),
        "scores": decode_scores(init_element.attrib["ten"]),
        "tehais": tehais,
    }


def convert_xml_to_mjai_events(xml_path: Path) -> list[dict[str, object]]:
    """读取单个天凤 XML 牌谱，按时间顺序生成完整 mjai 事件流。"""
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError as exc:
        raise ConversionError(f"xml parse error: {exc}") from exc

    events: list[dict[str, object]] = []
    last_draw_tile_id: list[int | None] = [None, None, None, None]
    names = ["", "", "", ""]
    start_game_emitted = False
    pending_end_kyoku = False
    saw_kyoku = False

    for child in root:
        tag = child.tag

        if tag == "GO":
            game_type = int(child.attrib.get("type", "0"))
            if game_type & 0x10:
                raise ConversionError("three-player paipu is not supported")
            continue

        if tag == "UN":
            names = parse_names(child)
            continue

        if tag == "TAIKYOKU":
            if not start_game_emitted:
                events.append({"type": "start_game", "names": names})
                start_game_emitted = True
            continue

        if tag == "INIT":
            if pending_end_kyoku:
                events.append({"type": "end_kyoku"})
                pending_end_kyoku = False
            if not start_game_emitted:
                events.append({"type": "start_game", "names": names})
                start_game_emitted = True
            events.append(build_start_kyoku_event(child))
            last_draw_tile_id = [None, None, None, None]
            saw_kyoku = True
            continue

        if len(tag) >= 2 and tag[0] in DRAW_TAG_TO_ACTOR and tag[1:].isdigit():
            actor = DRAW_TAG_TO_ACTOR[tag[0]]
            tile_id = int(tag[1:])
            last_draw_tile_id[actor] = tile_id
            events.append({"type": "tsumo", "actor": actor, "pai": tile_id_to_mjai(tile_id)})
            continue

        if len(tag) >= 2 and tag[0] in DISCARD_TAG_TO_ACTOR and tag[1:].isdigit():
            actor = DISCARD_TAG_TO_ACTOR[tag[0]]
            tile_id = int(tag[1:])
            events.append(
                {
                    "type": "dahai",
                    "actor": actor,
                    "pai": tile_id_to_mjai(tile_id),
                    "tsumogiri": last_draw_tile_id[actor] == tile_id,
                }
            )
            last_draw_tile_id[actor] = None
            continue

        if tag == "N":
            actor = int(child.attrib["who"])
            events.append(decode_meld(int(child.attrib["m"]), actor))
            last_draw_tile_id[actor] = None
            continue

        if tag == "DORA":
            events.append({"type": "dora", "dora_marker": tile_id_to_mjai(int(child.attrib["hai"]))})
            continue

        if tag == "REACH":
            actor = int(child.attrib["who"])
            step = int(child.attrib["step"])
            if step == 1:
                events.append({"type": "reach", "actor": actor})
            elif step == 2:
                events.append({"type": "reach_accepted", "actor": actor})
            else:
                raise ConversionError(f"unexpected reach step: {step}")
            continue

        if tag == "AGARI":
            hora_event: dict[str, object] = {
                "type": "hora",
                "actor": int(child.attrib["who"]),
                "target": int(child.attrib["fromWho"]),
            }
            deltas = parse_sc_deltas(child.attrib.get("sc"))
            if deltas is not None:
                hora_event["deltas"] = deltas
            ura_markers = decode_tiles(child.attrib.get("doraHaiUra", ""))
            if ura_markers:
                hora_event["ura_markers"] = ura_markers
            events.append(hora_event)
            pending_end_kyoku = True
            continue

        if tag == "RYUUKYOKU":
            ryukyoku_event: dict[str, object] = {"type": "ryukyoku"}
            deltas = parse_sc_deltas(child.attrib.get("sc"))
            if deltas is not None:
                ryukyoku_event["deltas"] = deltas
            events.append(ryukyoku_event)
            pending_end_kyoku = True
            continue

    if pending_end_kyoku:
        events.append({"type": "end_kyoku"})
    if saw_kyoku:
        events.append({"type": "end_game"})

    if not events:
        raise ConversionError("no events decoded")
    return events


def write_mjai_events(output_path: Path, events: Iterable[dict[str, object]]) -> int:
    """把 mjai 事件逐行写出为 JSON Lines，并返回写入的事件数。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    line_count = 0
    with output_path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
            line_count += 1
    return line_count


@dataclass(slots=True)
class FileTask:
    year: int
    input_path: str
    output_path: str
    overwrite: bool


@dataclass(slots=True)
class FileResult:
    year: int
    status: str
    elapsed_seconds: float
    input_path: str
    output_path: str
    event_count: int = 0
    error: str = ""


def convert_one_file(task: FileTask) -> FileResult:
    """执行单文件转换，供多进程 worker 调用并返回结构化结果。"""
    start_time = time.perf_counter()
    input_path = Path(task.input_path)
    output_path = Path(task.output_path)

    if output_path.exists() and not task.overwrite:
        return FileResult(
            year=task.year,
            status="skipped",
            elapsed_seconds=time.perf_counter() - start_time,
            input_path=task.input_path,
            output_path=task.output_path,
        )

    try:
        events = convert_xml_to_mjai_events(input_path)
        event_count = write_mjai_events(output_path, events)
        return FileResult(
            year=task.year,
            status="ok",
            elapsed_seconds=time.perf_counter() - start_time,
            input_path=task.input_path,
            output_path=task.output_path,
            event_count=event_count,
        )
    except Exception as exc:  # noqa: BLE001
        return FileResult(
            year=task.year,
            status="error",
            elapsed_seconds=time.perf_counter() - start_time,
            input_path=task.input_path,
            output_path=task.output_path,
            error=f"{exc.__class__.__name__}: {exc}",
        )


def iter_input_files(directory: Path) -> list[Path]:
    """递归收集目录下全部 XML 文件，供批量任务构建使用。"""
    if not directory.exists():
        return []
    return sorted(path for path in directory.rglob("*.xml") if path.is_file())


def format_duration(seconds: float) -> str:
    """把秒数格式化成 HH:MM:SS，用于进度和统计输出。"""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def print_progress(
    processed: int,
    total: int,
    ok_count: int,
    skipped_count: int,
    error_count: int,
    start_time: float,
) -> None:
    """按固定周期输出批处理进度、速率和预估剩余时间。"""
    elapsed = time.perf_counter() - start_time
    rate = processed / elapsed if elapsed > 0 else 0.0
    eta = (total - processed) / rate if rate > 0 else math.inf
    eta_text = format_duration(eta) if math.isfinite(eta) else "--:--:--"
    print(
        (
            f"[{format_duration(elapsed)}] processed={processed}/{total} "
            f"ok={ok_count} skipped={skipped_count} error={error_count} "
            f"rate={rate:.1f} files/s eta={eta_text}"
        ),
        flush=True,
    )


def parse_years(years_arg: str | None) -> list[int]:
    """解析命令行里的年份范围表达式，限制在 2018..2025。"""
    if not years_arg:
        return list(range(2018, 2026))
    years: set[int] = set()
    for chunk in years_arg.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start_text, end_text = chunk.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if start > end:
                start, end = end, start
            years.update(range(start, end + 1))
        else:
            years.add(int(chunk))
    return sorted(year for year in years if 2018 <= year <= 2025)


def build_tasks(repo_root: Path, years: list[int], overwrite: bool) -> tuple[list[FileTask], dict[int, int]]:
    """根据年份列表构造输入输出文件映射和每年的发现数量统计。"""
    tasks: list[FileTask] = []
    discovered_per_year: dict[int, int] = {}

    for year in years:
        input_dir = repo_root / "paipu" / f"paipu-{year}"
        output_dir = repo_root / "paipu-mjai" / f"paipu-mjai-{year}"
        files = iter_input_files(input_dir)
        discovered_per_year[year] = len(files)
        for input_path in files:
            relative_path = input_path.relative_to(input_dir)
            output_path = output_dir / relative_path.with_suffix(MJAI_SUFFIX)
            tasks.append(
                FileTask(
                    year=year,
                    input_path=str(input_path),
                    output_path=str(output_path),
                    overwrite=overwrite,
                )
            )

    return tasks, discovered_per_year


def print_summary(
    years: list[int],
    discovered_per_year: dict[int, int],
    ok_per_year: dict[int, int],
    skipped_per_year: dict[int, int],
    error_results: list[FileResult],
    total_events: int,
    start_time: float,
) -> None:
    """输出批处理结束后的汇总报告和错误样本。"""
    elapsed = time.perf_counter() - start_time
    print("\n=== Conversion Summary ===", flush=True)
    print(f"elapsed: {format_duration(elapsed)}", flush=True)
    print(f"total_events_written: {total_events}", flush=True)
    print("per_year:", flush=True)
    for year in years:
        discovered = discovered_per_year.get(year, 0)
        ok_count = ok_per_year.get(year, 0)
        skipped_count = skipped_per_year.get(year, 0)
        error_count = sum(1 for result in error_results if result.year == year)
        print(
            (
                f"  {year}: discovered={discovered} ok={ok_count} "
                f"skipped={skipped_count} error={error_count}"
            ),
            flush=True,
        )

    if error_results:
        print("errors:", flush=True)
        for result in error_results[:20]:
            print(f"  [{result.year}] {result.input_path}: {result.error}", flush=True)
        remaining = len(error_results) - 20
        if remaining > 0:
            print(f"  ... and {remaining} more errors", flush=True)


def main(argv: list[str] | None = None) -> int:
    """命令行入口：解析参数、调度多进程转换并输出统计信息。"""
    parser = argparse.ArgumentParser(
        description="Convert Tenhou XML paipu directories into mjai JSONL logs."
    )
    parser.add_argument(
        "--years",
        help="Years to convert, e.g. '2018-2025' or '2022,2024-2025'. Default: all years 2018-2025.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Project root containing paipu/ and paipu-mjai/ directories.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=max(1, (os.cpu_count() or 1) - 1),
        help="Worker process count. Default: cpu_count - 1.",
    )
    parser.add_argument(
        "--chunksize",
        type=int,
        default=50,
        help="imap_unordered chunksize. Increase for large corpora.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing output files instead of skipping them.",
    )
    args = parser.parse_args(argv)

    years = parse_years(args.years)
    if not years:
        print("No valid years selected in 2018-2025.", file=sys.stderr)
        return 2

    repo_root = args.repo_root.resolve()
    tasks, discovered_per_year = build_tasks(repo_root, years, args.overwrite)
    total_files = len(tasks)
    print(f"repo_root={repo_root}", flush=True)
    print(f"years={','.join(str(year) for year in years)}", flush=True)
    for year in years:
        input_dir = repo_root / "paipu" / f"paipu-{year}"
        output_dir = repo_root / "paipu-mjai" / f"paipu-mjai-{year}"
        print(
            (
                f"{year}: input={input_dir} output={output_dir} "
                f"files={discovered_per_year.get(year, 0)}"
            ),
            flush=True,
        )

    if total_files == 0:
        print("No XML files found. Nothing to do.", flush=True)
        return 0

    start_time = time.perf_counter()
    processed = 0
    ok_count = 0
    skipped_count = 0
    error_count = 0
    total_events = 0
    ok_per_year = {year: 0 for year in years}
    skipped_per_year = {year: 0 for year in years}
    error_results: list[FileResult] = []
    last_progress_time = start_time

    with mp.Pool(processes=max(1, args.workers)) as pool:
        for result in pool.imap_unordered(convert_one_file, tasks, chunksize=max(1, args.chunksize)):
            processed += 1
            total_events += result.event_count

            if result.status == "ok":
                ok_count += 1
                ok_per_year[result.year] = ok_per_year.get(result.year, 0) + 1
            elif result.status == "skipped":
                skipped_count += 1
                skipped_per_year[result.year] = skipped_per_year.get(result.year, 0) + 1
            else:
                error_count += 1
                error_results.append(result)

            now = time.perf_counter()
            if now - last_progress_time >= PROGRESS_INTERVAL_SECONDS or processed == total_files:
                print_progress(
                    processed=processed,
                    total=total_files,
                    ok_count=ok_count,
                    skipped_count=skipped_count,
                    error_count=error_count,
                    start_time=start_time,
                )
                last_progress_time = now

    print_summary(
        years=years,
        discovered_per_year=discovered_per_year,
        ok_per_year=ok_per_year,
        skipped_per_year=skipped_per_year,
        error_results=error_results,
        total_events=total_events,
        start_time=start_time,
    )

    return 1 if error_count else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(1)
