"""和风天气查询：城市解析、各时间粒度数据、中文格式化与缓存。

接口使用和风新版 API Host + ``X-QW-Api-Key``。分钟级降水和生活指数仍是
``/v7`` 路径，其余为 ``/v1``。模块不依赖 ``tools.py``，避免循环导入。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from nonebot.log import logger

from . import config


_ATTRIBUTION = "数据来源：和风天气（https://www.qweather.com）"
_KINDS = (
    "current",
    "minutely",
    "hourly",
    "daily",
    "alerts",
    "indices",
    "air",
    "air_daily",
)
_RESULT_CACHE_MAX = 256
_GEO_CACHE_TTL = 86400

_COORD_RE = re.compile(
    r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$"
)
_LOCATION_ID_RE = re.compile(r"^\d{6,12}$")

_NAME_SUFFIXES = (
    "特别行政区",
    "维吾尔自治区",
    "壮族自治区",
    "回族自治区",
    "自治区",
    "自治州",
    "地区",
    "新区",
    "市辖区",
    "省",
    "市",
    "区",
    "县",
    "旗",
    "盟",
)
_TYPE_PRIORITY = {
    "city": 3,
    "district": 2,
    "county": 2,
    "town": 1,
}
_COMPASS = {
    "n": "北风",
    "nne": "北东北风",
    "ne": "东北风",
    "ene": "东东北风",
    "e": "东风",
    "ese": "东东南风",
    "se": "东南风",
    "sse": "南东南风",
    "s": "南风",
    "ssw": "南西南风",
    "sw": "西南风",
    "wsw": "西西南风",
    "w": "西风",
    "wnw": "西西北风",
    "nw": "西北风",
    "nnw": "北西北风",
}
_SEVERITY = {
    "unknown": "未知",
    "minor": "一般",
    "moderate": "较重",
    "severe": "严重",
    "extreme": "特别严重",
}
_COLOR = {
    "white": "白色",
    "gray": "灰色",
    "green": "绿色",
    "blue": "蓝色",
    "yellow": "黄色",
    "amber": "琥珀色",
    "orange": "橙色",
    "red": "红色",
    "purple": "紫色",
    "black": "黑色",
}
_PRECIP_TYPE = {
    "rain": "雨",
    "snow": "雪",
    "ice": "冰",
    "mixed": "雨夹雪",
    "none": "无",
    "unknown": "未知",
}

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = asyncio.Lock()
_USAGE_PATH = Path(__file__).resolve().parents[3] / "data" / "weather_usage.json"
_USAGE_LOCK = asyncio.Lock()
_USAGE_MEMORY: tuple[str, int] | None = None


class WeatherError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | str | None = None,
        no_data: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.no_data = no_data


@dataclass(frozen=True)
class Location:
    name: str
    location_id: str
    lat: float
    lon: float
    adm1: str = ""
    adm2: str = ""
    country: str = ""
    tz: str = ""
    loc_type: str = ""
    rank: float = 0.0
    ambiguous: bool = False
    admin_mismatch: bool = False
    requested_adm: str = ""
    approximate: bool = False


# ─── 请求与缓存 ──────────────────────────────────────────────


def _cache_key(path: str, params: dict[str, Any]) -> str:
    pairs = sorted((str(key), str(value)) for key, value in params.items())
    return path + "?" + "&".join(f"{key}={value}" for key, value in pairs)


async def _cache_get(key: str) -> Any | None:
    async with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        expires, value = entry
        if expires <= time.monotonic():
            _CACHE.pop(key, None)
            return None
        return value


async def _cache_put(key: str, ttl: int, value: Any) -> None:
    if ttl <= 0:
        return
    async with _CACHE_LOCK:
        if len(_CACHE) >= _RESULT_CACHE_MAX:
            now = time.monotonic()
            for expired in [k for k, (exp, _) in _CACHE.items() if exp <= now]:
                _CACHE.pop(expired, None)
            while len(_CACHE) >= _RESULT_CACHE_MAX:
                _CACHE.pop(next(iter(_CACHE)), None)
        _CACHE[key] = (time.monotonic() + ttl, value)


def _month_key() -> str:
    return datetime.now().astimezone().strftime("%Y-%m")


def _load_usage() -> dict[str, Any]:
    try:
        raw = _USAGE_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning(f"天气用量记录读取失败：{e}")
        return {}


def _save_usage(month: str, count: int) -> None:
    try:
        _USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _USAGE_PATH.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"month": month, "count": count}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, _USAGE_PATH)
    except OSError as e:
        logger.warning(f"天气用量记录写入失败：{e}")


async def _consume_request() -> None:
    """在真正发出请求前占用一次月度额度；0 表示不限制。"""
    global _USAGE_MEMORY
    limit = config.AI_WEATHER_MONTHLY_LIMIT
    if limit <= 0:
        return

    month = _month_key()
    async with _USAGE_LOCK:
        data = _load_usage()
        count = 0
        if data.get("month") == month:
            raw = data.get("count")
            if not isinstance(raw, bool):
                try:
                    count = max(0, int(raw))
                except (TypeError, ValueError):
                    count = 0
        if _USAGE_MEMORY is not None and _USAGE_MEMORY[0] == month:
            count = max(count, _USAGE_MEMORY[1])
        if count >= limit:
            logger.warning(f"天气查询月度额度已用完：{count}/{limit}")
            raise WeatherError(
                f"本月天气查询额度已用完（{limit} 次），"
                "请下月再试或调整 AI_WEATHER_MONTHLY_LIMIT。",
                status="quota",
            )
        count += 1
        _USAGE_MEMORY = (month, count)
        _save_usage(month, count)


def _status_message(status: int | str) -> str:
    try:
        code = int(status)
    except (TypeError, ValueError):
        return f"和风天气返回错误码 {status}。"
    if code == 204:
        return "该地区暂无这项天气数据。"
    if code == 400:
        return "城市或查询参数无效，请换一个地名或补充省份。"
    if code == 401:
        return "API Key 无效（AI_WEATHER_API_KEY）。"
    if code == 402:
        return "和风天气账户额度不足。"
    if code == 403:
        return "请求被拒绝，请检查 API Host、Key 权限和账户额度。"
    if code == 404:
        return "没有找到对应的城市或天气数据。"
    if code == 429:
        return "请求过于频繁或额度已用完，请稍后再试。"
    if code == 408 or code >= 500:
        return "和风天气服务暂时不可用，请稍后再试。"
    return f"和风天气返回 HTTP {code}。"


def _legacy_code_message(code: str) -> str:
    return {
        "204": "该地区暂无这项天气数据。",
        "400": "城市或查询参数无效，请换一个地名或补充省份。",
        "401": "API Key 无效（AI_WEATHER_API_KEY）。",
        "402": "和风天气账户额度不足。",
        "403": "请求被拒绝，请检查 API Host、Key 权限和账户额度。",
        "404": "没有找到对应的城市或天气数据。",
        "429": "请求过于频繁或额度已用完，请稍后再试。",
        "500": "和风天气服务暂时不可用，请稍后再试。",
    }.get(code, f"和风天气返回错误码 {code}。")


def _error_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return (response.text or "").strip()
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            parts = [
                str(error.get(name) or "")
                for name in ("title", "detail", "type", "invalidParams")
            ]
            return " ".join(part for part in parts if part)
        return str(data.get("message") or data.get("code") or "")
    return ""


async def _request(path: str, params: dict[str, Any]) -> dict[str, Any]:
    if not config.AI_WEATHER_API_KEY or not config.AI_WEATHER_API_HOST:
        raise WeatherError(
            "天气查询未配置（AI_WEATHER_API_KEY / AI_WEATHER_API_HOST），"
            "暂时无法使用。"
        )

    url = f"{config.AI_WEATHER_API_HOST}{path}"
    headers = {
        "X-QW-Api-Key": config.AI_WEATHER_API_KEY,
        "Accept": "application/json",
        "User-Agent": "SeatideBot/0.1 (+qweather)",
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(config.AI_WEATHER_TIMEOUT)
        ) as client:
            response = await client.get(url, params=params, headers=headers)
    except httpx.TimeoutException:
        logger.warning(f"和风天气请求超时：{path}")
        raise WeatherError("和风天气请求超时，请稍后再试。") from None
    except httpx.HTTPError as e:
        logger.warning(
            f"和风天气网络错误：{path}（{e.__class__.__name__}：{e}）"
        )
        raise WeatherError(
            f"和风天气网络错误（{e.__class__.__name__}）。"
        ) from None

    if response.status_code != 200:
        detail = _error_detail(response)
        logger.warning(
            f"和风天气请求失败：{path} HTTP {response.status_code}："
            f"{detail[:300]}"
        )
        raise WeatherError(
            _status_message(response.status_code),
            status=response.status_code,
            no_data=response.status_code == 204,
        )

    try:
        data = response.json()
    except ValueError:
        logger.warning(f"和风天气返回无法解析的内容：{path}")
        raise WeatherError("和风天气返回了无法解析的内容。") from None
    if not isinstance(data, dict):
        raise WeatherError("和风天气返回格式不符合预期。")

    error = data.get("error")
    if isinstance(error, dict):
        detail = _error_detail(response)
        logger.warning(f"和风天气错误：{path}：{detail[:300]}")
        status = error.get("status") or response.status_code
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = response.status_code
        if 200 <= status < 300:
            status = 500
        raise WeatherError(
            _status_message(status),
            status=status,
            no_data=status == 204,
        )

    code = data.get("code")
    if code is not None and str(code) != "200":
        code_text = str(code)
        detail = str(data.get("message") or "")
        logger.warning(
            f"和风天气错误码：{path} code={code_text}：{detail[:300]}"
        )
        raise WeatherError(
            _legacy_code_message(code_text),
            status=code_text,
            no_data=code_text == "204",
        )
    return data


async def _get_json(path: str, params: dict[str, Any], ttl: int) -> Any:
    key = _cache_key(path, params)
    if ttl > 0:
        cached = await _cache_get(key)
        if cached is not None:
            return cached

    await _consume_request()
    data = await _request(path, params)
    await _cache_put(key, ttl, data)
    return data


async def _geo_lookup(params: dict[str, Any]) -> tuple[Location, ...]:
    try:
        payload = await _get_json(
            "/geo/v2/city/lookup", params, _GEO_CACHE_TTL
        )
    except WeatherError as e:
        if e.no_data:
            return ()
        raise
    return _parse_locations(payload)


# ─── 城市解析 ────────────────────────────────────────────────


def _norm_name(value: str) -> str:
    text = re.sub(r"\s+", "", str(value or "")).lower()
    for suffix in _NAME_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)]
    return text


def _parse_locations(payload: Any) -> tuple[Location, ...]:
    data = payload if isinstance(payload, dict) else {}
    raw = data.get("location")
    if isinstance(raw, dict):
        raw = [raw]
    result: list[Location] = []
    if not isinstance(raw, list):
        return ()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        lat = _number(item.get("lat"))
        lon = _number(item.get("lon"))
        if not name or lat is None or lon is None:
            continue
        result.append(
            Location(
                name=name,
                location_id=str(item.get("id") or ""),
                lat=lat,
                lon=lon,
                adm1=str(item.get("adm1") or "").strip(),
                adm2=str(item.get("adm2") or "").strip(),
                country=str(item.get("country") or "").strip(),
                tz=str(item.get("tz") or "").strip(),
                loc_type=str(item.get("type") or "").strip().lower(),
                rank=_number(item.get("rank")) or 0.0,
            )
        )
    return tuple(result)


def _score_location(
    loc: Location, query: str, adm: str
) -> tuple[float, int, float, Location] | None:
    name = _norm_name(loc.name)
    adm1 = _norm_name(loc.adm1)
    adm2 = _norm_name(loc.adm2)
    score: float | None = None
    if query and name == query:
        score = 100
    elif query and len(query) >= 2 and name.startswith(query):
        score = 80
    elif query and query in name:
        score = 65
    elif query and len(name) >= 2 and name in query:
        score = 45
    elif query and query in adm2:
        score = 35
    elif query and query in adm1:
        score = 20
    if score is None:
        return None

    if adm:
        if adm in {adm1, adm2} or adm in f"{adm1}/{adm2}":
            score += 30
        else:
            score -= 5

    priority = _TYPE_PRIORITY.get(loc.loc_type, 0)
    return (score, priority, loc.rank, loc)


def _pick_location(
    query: str, adm: str | None, locations: tuple[Location, ...]
) -> Location | None:
    query_norm = _norm_name(query)
    adm_norm = _norm_name(adm or "")
    scored: list[tuple[float, int, float, Location]] = []
    for loc in locations:
        country = loc.country.strip()
        if (
            country
            and "中国" not in country
            and country.lower() not in {"cn", "china"}
        ):
            continue
        item = _score_location(loc, query_norm, adm_norm)
        if item is not None:
            scored.append(item)
    if not scored:
        return None

    scored.sort(key=lambda item: (item[0], item[2], item[1]), reverse=True)
    best_score, _, _, best = scored[0]
    exact_count = sum(
        1 for item in scored if _norm_name(item[3].name) == query_norm
    )
    adm_confident = bool(adm_norm) and (
        adm_norm in _norm_name(best.adm1)
        or adm_norm in _norm_name(best.adm2)
    )
    ambiguous = (
        best_score < 90
        or (exact_count > 1 and not adm_confident)
        or sum(1 for item in scored if item[0] == best_score) > 1
    )
    approximate = best.loc_type in {"province", "country"}
    return replace(best, ambiguous=ambiguous, approximate=approximate)


def _adm_matches(loc: Location, adm: str) -> bool:
    target = _norm_name(adm)
    return bool(target) and (
        target in _norm_name(loc.adm1) or target in _norm_name(loc.adm2)
    )


async def resolve_city(
    city: str, adm: str | None = None
) -> Location:
    raw = str(city or "").strip()
    if not raw:
        raise WeatherError(
            "请提供城市名，例如“北京”“杭州”“浦东”。"
        )
    if len(raw) > 100:
        raise WeatherError("城市名过长，请换一个更简洁的名称。")

    coord = _COORD_RE.match(raw)
    if coord is not None:
        lon = float(coord.group(1))
        lat = float(coord.group(2))
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise WeatherError("经纬度超出范围。")
        try:
            locations = await _geo_lookup(
                {
                    "location": f"{lon:.2f},{lat:.2f}",
                    "range": "cn",
                    "number": 1,
                    "lang": "zh",
                }
            )
        except WeatherError as e:
            logger.warning(f"天气坐标反查失败（使用坐标）：{e}")
            locations = ()
        if locations:
            return replace(locations[0], lat=lat, lon=lon)
        return Location(
            name=f"{lon:.2f},{lat:.2f}",
            location_id="",
            lat=lat,
            lon=lon,
            country="中国",
        )

    if _LOCATION_ID_RE.match(raw):
        locations = await _geo_lookup(
            {
                "location": raw,
                "range": "cn",
                "number": 1,
                "lang": "zh",
            }
        )
        if not locations:
            raise WeatherError(
                f"没有找到 LocationID {raw} 对应的城市。"
            )
        return locations[0]

    params: dict[str, Any] = {
        "location": raw,
        "range": "cn",
        "number": 20,
        "lang": "zh",
    }
    adm_text = str(adm or "").strip()
    if adm_text:
        params["adm"] = adm_text[:50]

    locations = await _geo_lookup(params)
    admin_mismatch = False
    if adm_text and not locations:
        admin_mismatch = True
        params.pop("adm", None)
        locations = await _geo_lookup(params)

    if not locations:
        raise WeatherError(
            f"没有找到「{raw}」对应的中国大陆城市，"
            "请换一个城市名或补充省份。"
        )

    loc = _pick_location(raw, adm_text or None, locations)
    if loc is None:
        raise WeatherError(
            f"没有找到「{raw}」对应的中国大陆城市，"
            "请换一个城市名或补充省份。"
        )
    if adm_text and not _adm_matches(loc, adm_text):
        admin_mismatch = True
    if admin_mismatch:
        loc = replace(
            loc, admin_mismatch=True, requested_adm=adm_text
        )
    return loc


# ─── 格式化工具 ──────────────────────────────────────────────


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _num(value: float | None, digits: int = 0) -> str:
    if value is None:
        return "--"
    if digits <= 0:
        return str(int(round(value)))
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return text or "0"


def _measure(
    value: Any, default_unit: str = "", digits: int = 1
) -> str:
    if isinstance(value, dict):
        number = _number(value.get("value"))
        unit = str(value.get("unit") or default_unit)
    else:
        number = _number(value)
        unit = default_unit
    if number is None:
        return ""
    return f"{_num(number, digits)}{unit}"


def _fmt_temp(value: Any) -> str:
    data = _dict(value)
    number = _number(data.get("value") if data else value)
    if number is None:
        return "未知"
    unit = str(data.get("unit") or "°C")
    return f"{_num(number)}{unit}"


def _fmt_pct(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "--"
    if 0 <= number <= 1:
        number *= 100
    return f"{_num(number)}%"


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _fmt_time(value: Any, fmt: str = "%m-%d %H:%M") -> str:
    dt = _parse_time(value)
    if dt is None:
        return str(value or "").strip()
    return dt.strftime(fmt)


def _weekday(dt: datetime) -> str:
    return "一二三四五六日"[dt.weekday()]


def _fmt_date(value: Any) -> str:
    text = str(value or "").strip()
    dt = _parse_time(text)
    if dt is not None:
        return f"{dt.strftime('%m-%d')} 周{_weekday(dt)}"
    return text


def _condition(value: Any) -> str:
    return str(_dict(value).get("text") or "").strip()


def _compass_zh(direction: Any) -> str:
    data = _dict(direction)
    degree = _number(data.get("degree"))
    if degree is not None:
        names = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")
        index = int((degree + 22.5) // 45) % 8
        return f"{names[index]}风"
    code = str(data.get("compass") or "").strip().lower()
    return _COMPASS.get(code, "风向不定")


def _fmt_wind(value: Any) -> str:
    wind = _dict(value)
    if not wind:
        return ""
    text = _compass_zh(wind.get("direction"))
    scale = _number(wind.get("scale"))
    speed = _measure(wind.get("speed"), digits=1)
    if scale is not None:
        text += f"{_num(scale)}级"
    if speed:
        text += f"（{speed}）"
    return text


def _fmt_visibility(value: Any) -> str:
    data = _dict(value)
    number = _number(data.get("value"))
    unit = str(data.get("unit") or "").strip()
    if number is None:
        return ""
    if unit.lower() == "m" and number >= 1000:
        return f"{_num(number / 1000, 1)}km"
    return _measure(value, digits=0)


def _fmt_precip(value: Any, *, probability: bool = False) -> str:
    data = _dict(value)
    parts: list[str] = []
    if probability:
        prob = _fmt_pct(data.get("probability"))
        if prob != "--":
            parts.append(f"概率{prob}")
    amount = _measure(data.get("amount"), "mm", digits=2)
    if amount:
        parts.append("无降水" if amount in {"0mm", "0.0mm"} else amount)
    if not amount:
        kind = _PRECIP_TYPE.get(str(data.get("type") or "").lower(), "")
        if kind and kind not in {"无", "未知"}:
            parts.append(kind)
    return "，".join(parts)


def _truncate(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _location_title(loc: Location) -> str:
    admins: list[str] = []
    for value in (loc.adm1.strip(), loc.adm2.strip()):
        if value and value not in admins:
            admins.append(value)
    name = loc.name.strip()
    if name and not any(name in admin for admin in admins):
        admins.append(name)
    return " ".join(admins) if admins else (name or "未知地点")


def _location_header(loc: Location) -> str:
    text = f"匹配到：{_location_title(loc)}"
    notes: list[str] = []
    if loc.ambiguous:
        notes.append("如不对请补充省份")
    if loc.admin_mismatch:
        if loc.requested_adm:
            notes.append(f"{loc.requested_adm}下未找到，按名称匹配")
        else:
            notes.append("按名称匹配")
    if loc.approximate:
        notes.append("按省级中心点")
    if notes:
        text += "（" + "；".join(notes) + "）"
    return text


def _int_arg(
    value: Any, *, default: int, low: int, high: int, name: str
) -> int:
    if value is None or (
        isinstance(value, str) and not value.strip()
    ):
        return default
    if isinstance(value, bool):
        raise WeatherError(f"参数 {name} 必须是整数。")
    if isinstance(value, float):
        if not value.is_integer():
            raise WeatherError(f"参数 {name} 必须是整数。")
        number = int(value)
    elif isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            raise WeatherError(f"参数 {name} 必须是整数。") from None
        if not parsed.is_integer():
            raise WeatherError(f"参数 {name} 必须是整数。")
        number = int(parsed)
    else:
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise WeatherError(f"参数 {name} 必须是整数。") from None
    return min(high, max(low, number))


def _normalize_kind(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "current"
    if text not in _KINDS:
        raise WeatherError(
            f"查询类型无效：{text}；可选 {', '.join(_KINDS)}。"
        )
    return text


def _indices_days(value: Any) -> tuple[str, int]:
    number = _int_arg(value, default=1, low=1, high=3, name="days")
    if number >= 3:
        return ("3d", 3)
    return ("1d", 1)


def _normalize_index_types(value: Any) -> tuple[int, ...]:
    if value is None:
        return (3, 5, 9)
    if isinstance(value, bool):
        raise WeatherError("index_types 必须是 0~16 的整数。")
    if isinstance(value, int):
        values = [value]
    elif isinstance(value, float):
        if not value.is_integer():
            raise WeatherError("index_types 必须是 0~16 的整数。")
        values = [int(value)]
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return (3, 5, 9)
        values = []
        for part in text.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                values.append(int(part))
            except ValueError:
                raise WeatherError(
                    "index_types 必须是 0~16 的整数。"
                ) from None
    elif isinstance(value, list):
        values = []
        for item in value:
            if isinstance(item, bool):
                raise WeatherError(
                    "index_types 必须是 0~16 的整数。"
                )
            if isinstance(item, int):
                values.append(item)
            elif isinstance(item, float) and item.is_integer():
                values.append(int(item))
            elif isinstance(item, str):
                try:
                    values.append(int(item.strip()))
                except ValueError:
                    raise WeatherError(
                        "index_types 必须是 0~16 的整数。"
                    ) from None
            else:
                raise WeatherError(
                    "index_types 必须是 0~16 的整数。"
                )
    else:
        raise WeatherError("index_types 必须是 0~16 的整数。")

    if not values:
        return (3, 5, 9)
    result: list[int] = []
    for number in values:
        if not (0 <= number <= 16):
            raise WeatherError(
                "index_types 必须在 0~16 之间（0=全部）。"
            )
        if number not in result:
            result.append(number)
    if 0 in result:
        return (0,)
    return tuple(result)


def _lat(loc: Location) -> str:
    return f"{loc.lat:.2f}"


def _lon(loc: Location) -> str:
    return f"{loc.lon:.2f}"


# ─── 渲染：纯函数，便于用示例 JSON 自测 ─────────────────────


def _render_current(loc: Location, data: Any) -> str:
    current = _dict(data)
    lines = [_location_header(loc)]
    valid = _fmt_time(current.get("validTime"))
    lines.append(f"实时天气（{valid}）" if valid else "实时天气")

    condition = _condition(current.get("condition")) or "未知"
    temp = _fmt_temp(current.get("temperature"))
    feels = _fmt_temp(current.get("feelsLike"))
    line = condition
    if temp != "未知":
        line += f" {temp}"
    if feels != "未知" and feels != temp:
        line += f"（体感 {feels}）"
    lines.append(line)

    details: list[str] = []
    humidity = _fmt_pct(current.get("humidity"))
    if humidity != "--":
        details.append(f"湿度{humidity}")
    wind = _fmt_wind(current.get("wind"))
    if wind:
        details.append(wind)
    gust = _measure(current.get("windGust"), digits=1)
    if gust:
        details.append(f"阵风{gust}")
    precip = _fmt_precip(current.get("precipitation"))
    if precip:
        if precip in {"无降水", "雨", "雪", "冰", "雨夹雪"}:
            details.append(precip)
        else:
            details.append(f"降水{precip}")
    if details:
        lines.append("，".join(details))

    extras: list[str] = []
    pressure = _measure(current.get("pressure"), digits=1)
    if pressure:
        extras.append(f"气压{pressure}")
    visibility = _fmt_visibility(current.get("visibility"))
    if visibility:
        extras.append(f"能见度{visibility}")
    uv = _number(current.get("uvIndex"))
    if uv is not None:
        extras.append(f"紫外线{_num(uv)}")
    dew = _fmt_temp(current.get("dewPoint"))
    if dew != "未知":
        extras.append(f"露点{dew}")
    cloud = _fmt_pct(current.get("cloudCover"))
    if cloud != "--":
        extras.append(f"云量{cloud}")
    if extras:
        lines.append("，".join(extras))
    return "\n".join(lines)


def _render_hourly(loc: Location, data: Any, hours: int) -> str:
    entries = _dict_list(_dict(data).get("hours"))[:hours]
    if not entries:
        raise WeatherError("暂无逐小时预报数据。", no_data=True)

    step = 1 if hours <= 24 else (3 if hours <= 72 else 6)
    indexes = list(range(0, len(entries), step))
    if indexes[-1] != len(entries) - 1:
        indexes.append(len(entries) - 1)
    selected = [entries[index] for index in indexes]

    lines = [_location_header(loc)]
    note = f"，每 {step} 小时抽样" if step > 1 else ""
    lines.append(f"逐小时预报（未来 {hours} 小时{note}）")
    for item in selected:
        parts: list[str] = []
        stamp = _fmt_time(item.get("forecastTime"))
        if stamp:
            parts.append(stamp)
        parts.append(_condition(item.get("condition")) or "未知")
        temp = _fmt_temp(item.get("temperature"))
        if temp != "未知":
            parts.append(temp)
        feels = _fmt_temp(item.get("feelsLike"))
        if feels != "未知" and feels != temp:
            parts.append(f"体感{feels}")
        humidity = _fmt_pct(item.get("humidity"))
        if humidity != "--":
            parts.append(f"湿度{humidity}")
        wind = _fmt_wind(item.get("wind"))
        if wind:
            parts.append(wind)
        precip = _fmt_precip(item.get("precipitation"), probability=True)
        if precip:
            parts.append(precip)
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _render_daily(loc: Location, data: Any, days: int) -> str:
    entries = _dict_list(_dict(data).get("days"))[:days]
    if not entries:
        raise WeatherError("暂无逐日预报数据。", no_data=True)

    lines = [_location_header(loc)]
    lines.append(f"每日预报（未来 {len(entries)} 天）")
    for item in entries:
        day = _dict(item.get("daytime"))
        night = _dict(item.get("nighttime"))
        day_cond = _condition(day.get("condition"))
        night_cond = _condition(night.get("condition"))
        if day_cond and night_cond and day_cond != night_cond:
            condition = f"{day_cond}转{night_cond}"
        else:
            condition = day_cond or night_cond or "未知"

        dt = _parse_time(item.get("forecastStartTime"))
        date = dt.strftime("%m-%d") if dt else ""
        weekday = f" 周{_weekday(dt)}" if dt else ""
        tmax = _fmt_temp(item.get("temperatureMax"))
        tmin = _fmt_temp(item.get("temperatureMin"))
        if tmin != "未知" and tmax != "未知":
            temp = f"{tmin}~{tmax}"
        elif tmax != "未知":
            temp = f"最高{tmax}"
        elif tmin != "未知":
            temp = f"最低{tmin}"
        else:
            temp = ""

        precip = _fmt_precip(
            day.get("precipitation"), probability=True
        ) or _fmt_precip(night.get("precipitation"), probability=True)
        wind = _fmt_wind(day.get("wind")) or _fmt_wind(night.get("wind"))
        parts = [
            part
            for part in (f"{date}{weekday}".strip(), condition, temp, precip, wind)
            if part
        ]
        lines.append(" ".join(parts))

        astro = _dict(item.get("astro"))
        sunrise = _fmt_time(astro.get("sunrise"), "%H:%M")
        sunset = _fmt_time(astro.get("sunset"), "%H:%M")
        uv = _number(item.get("uvIndexMax"))
        extras: list[str] = []
        if sunrise:
            extras.append(f"日出{sunrise}")
        if sunset:
            extras.append(f"日落{sunset}")
        if uv is not None:
            extras.append(f"紫外线{_num(uv)}")
        if extras:
            lines.append("　" + "，".join(extras))
    return "\n".join(lines)


def _render_minutely(loc: Location, data: Any) -> str:
    payload = _dict(data)
    entries = _dict_list(payload.get("minutely"))
    summary = str(payload.get("summary") or "").strip()
    if not entries and not summary:
        raise WeatherError(
            "该地区暂无分钟级降水数据（仅支持中国大陆）。",
            no_data=True,
        )

    lines = [_location_header(loc)]
    updated = _fmt_time(payload.get("updateTime"), "%H:%M")
    lines.append(
        f"分钟级降水（未来 2 小时，更新 {updated}）"
        if updated
        else "分钟级降水（未来 2 小时）"
    )
    if summary:
        lines.append(summary)

    runs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for item in entries:
        amount = _number(item.get("precip")) or 0.0
        if amount > 0.001:
            stamp = _fmt_time(item.get("fxTime"), "%H:%M")
            kind = _PRECIP_TYPE.get(
                str(item.get("type") or "").lower(), "降水"
            )
            if current is None:
                current = {
                    "start": stamp,
                    "end": stamp,
                    "peak": amount,
                    "type": kind,
                }
            else:
                current["end"] = stamp
                current["peak"] = max(current["peak"], amount)
        elif current is not None:
            runs.append(current)
            current = None
    if current is not None:
        runs.append(current)

    if runs:
        for run in runs:
            lines.append(
                f"{run['start']}-{run['end']} 有{run['type']}，"
                f"峰值{_num(run['peak'], 2)}mm/5分钟"
            )
    elif not summary:
        lines.append("未来 2 小时没有降水")
    return "\n".join(lines)


def _render_alerts(
    loc: Location,
    data: Any,
    *,
    include_empty: bool,
    include_header: bool = True,
) -> str:
    payload = _dict(data)
    alerts = _dict_list(payload.get("alerts"))
    if not alerts:
        if not include_empty:
            return ""
        body = "当前没有生效中的天气预警。"
        return (
            f"{_location_header(loc)}\n{body}"
            if include_header
            else body
        )

    lines: list[str] = []
    if include_header:
        lines.append(_location_header(loc))
    lines.append(f"当前生效预警 {len(alerts)} 条")
    for item in alerts[:5]:
        event = _dict(item.get("eventType"))
        name = str(event.get("name") or "天气预警").strip()
        color = _COLOR.get(
            str(_dict(item.get("color")).get("code") or "").lower(), ""
        )
        severity = _SEVERITY.get(
            str(item.get("severity") or "").lower(), ""
        )
        headline = str(item.get("headline") or "").strip()
        title = headline or name
        meta = "，".join(
            part
            for part in (
                f"{color}预警" if color else "",
                severity,
            )
            if part
        )
        lines.append(f"[{title}]" + (f"（{meta}）" if meta else ""))

        start = _fmt_time(item.get("effectiveTime"))
        end = _fmt_time(item.get("expireTime"))
        if start or end:
            lines.append(f"生效 {start or '未知'}，失效 {end or '未知'}")
        description = _truncate(item.get("description"), 200)
        if description:
            lines.append(description)
        instruction = _truncate(item.get("instruction"), 150)
        if instruction:
            lines.append(f"防御指引：{instruction}")
    if len(alerts) > 5:
        lines.append(f"（仅显示前 5 条，共 {len(alerts)} 条）")
    return "\n".join(lines)


def _render_indices(loc: Location, data: Any, days: int) -> str:
    entries = _dict_list(_dict(data).get("daily"))
    if not entries:
        raise WeatherError("暂无生活指数数据。", no_data=True)

    groups: dict[str, list[dict[str, Any]]] = {}
    for item in entries:
        groups.setdefault(str(item.get("date") or ""), []).append(item)

    lines = [_location_header(loc)]
    for date, items in list(groups.items())[:days]:
        lines.append(f"生活指数（{_fmt_date(date)}）" if date else "生活指数")
        for item in items:
            name = str(item.get("name") or "").strip()
            if not name:
                name = f"指数{item.get('type') or ''}".strip()
            category = str(item.get("category") or "").strip()
            level = _number(item.get("level"))
            text = _truncate(item.get("text"), 100)
            line = f"{name}：{category}" if category else name
            if level is not None:
                line += f"（{_num(level)}级）"
            if text:
                line += f"，{text}"
            lines.append(f"- {line}")
    return "\n".join(lines)


def _pick_aqi(indexes: list[dict[str, Any]]) -> dict[str, Any] | None:
    for index in indexes:
        if str(index.get("code") or "").lower() == "qaqi":
            return index
    for index in indexes:
        code = str(index.get("code") or "").lower()
        name = str(index.get("name") or "")
        if "cn" in code or "中国" in name:
            return index
    for index in indexes:
        if _number(index.get("aqi")) is not None:
            return index
    return indexes[0] if indexes else None


def _render_air(loc: Location, data: Any) -> str:
    payload = _dict(data)
    indexes = _dict_list(payload.get("indexes"))
    if not indexes:
        raise WeatherError("暂无空气质量数据。", no_data=True)
    index = _pick_aqi(indexes)
    if index is None:
        raise WeatherError("暂无空气质量数据。", no_data=True)

    lines = [_location_header(loc)]
    observed = _fmt_time(payload.get("validTime"))
    if not observed:
        observed = datetime.now().astimezone().strftime("%m-%d %H:%M")
    lines.append(f"实时空气质量（{observed}）")

    name = str(index.get("name") or "AQI")
    aqi = index.get("aqiDisplay")
    if aqi in (None, ""):
        aqi = index.get("aqi")
    if aqi in (None, ""):
        aqi = "未知"
    category = str(index.get("category") or "").strip()
    primary = str(
        _dict(index.get("primaryPollutant")).get("name") or ""
    ).strip()
    line = f"{name} {aqi}"
    if category:
        line += f" {category}"
    if primary:
        line += f"，首要污染物 {primary}"
    lines.append(line)

    pollutants = _dict_list(payload.get("pollutants"))
    by_code = {
        str(item.get("code") or "").lower(): item
        for item in pollutants
    }
    parts: list[str] = []
    for code in ("pm2p5", "pm10", "o3", "no2", "so2", "co"):
        item = by_code.get(code)
        if not item:
            continue
        label = str(item.get("name") or code)
        concentration = _measure(item.get("concentration"), digits=1)
        if concentration:
            parts.append(f"{label} {concentration}")
    if parts:
        lines.append("，".join(parts))

    health = _dict(index.get("health"))
    advice = _dict(health.get("advice"))
    text = str(
        advice.get("generalPopulation")
        or advice.get("sensitivePopulation")
        or health.get("effect")
        or ""
    ).strip()
    if text:
        lines.append(f"建议：{_truncate(text, 120)}")
    return "\n".join(lines)


def _render_air_daily(loc: Location, data: Any, days: int) -> str:
    entries = _dict_list(_dict(data).get("days"))[:days]
    if not entries:
        raise WeatherError("暂无空气质量预报数据。", no_data=True)

    lines = [_location_header(loc)]
    lines.append(f"空气质量预报（未来 {len(entries)} 天）")
    for item in entries:
        dt = _parse_time(item.get("forecastStartTime"))
        date = f"{dt.strftime('%m-%d')} 周{_weekday(dt)}" if dt else ""
        indexes = _dict_list(item.get("indexes"))
        index = _pick_aqi(indexes)
        if index is None:
            lines.append(f"{date}：暂无数据" if date else "暂无数据")
            continue
        aqi = index.get("aqiDisplay")
        if aqi in (None, ""):
            aqi = index.get("aqi")
        if aqi in (None, ""):
            aqi = "未知"
        category = str(index.get("category") or "").strip()
        primary = str(
            _dict(index.get("primaryPollutant")).get("name") or ""
        ).strip()
        line = f"{date}：AQI {aqi}" if date else f"AQI {aqi}"
        if category:
            line += f" {category}"
        if primary:
            line += f"，首要污染物 {primary}"
        lines.append(line)
    return "\n".join(lines)


# ─── 取数 + 渲染 ────────────────────────────────────────────


async def _query_current(loc: Location) -> str:
    lat, lon = _lat(loc), _lon(loc)
    current, alerts = await asyncio.gather(
        _get_json(
            f"/weather/v1/current/{lat}/{lon}",
            {"localTime": "true", "lang": "zh"},
            config.AI_WEATHER_CACHE_TTL,
        ),
        _get_json(
            f"/weatheralert/v1/current/{lat}/{lon}",
            {"localTime": "true", "lang": "zh"},
            config.AI_WEATHER_CACHE_TTL,
        ),
        return_exceptions=True,
    )
    if isinstance(current, Exception):
        if isinstance(current, WeatherError):
            raise current
        raise WeatherError("天气查询失败。") from current
    if not _dict(current):
        raise WeatherError("暂无实时天气数据。", no_data=True)

    body = _render_current(loc, current)
    if not isinstance(alerts, Exception):
        alert_text = _render_alerts(
            loc, alerts, include_empty=False, include_header=False
        )
        if alert_text:
            body += "\n\n" + alert_text
    else:
        logger.warning(f"天气预警查询失败（忽略）：{alerts}")
    return body


async def _query_minutely(loc: Location) -> str:
    try:
        data = await _get_json(
            "/v7/minutely/5m",
            {"location": f"{_lon(loc)},{_lat(loc)}", "lang": "zh"},
            config.AI_WEATHER_CACHE_TTL,
        )
    except WeatherError as e:
        if e.no_data or str(e.status) == "400":
            raise WeatherError(
                "该地区暂无分钟级降水数据（仅支持中国大陆）。",
                no_data=True,
            ) from None
        raise
    return _render_minutely(loc, data)


async def _query_hourly(loc: Location, hours: Any) -> str:
    count = _int_arg(
        hours, default=24, low=1, high=240, name="hours"
    )
    data = await _get_json(
        f"/weather/v1/hourly/{_lat(loc)}/{_lon(loc)}",
        {
            "hours": str(count),
            "localTime": "true",
            "lang": "zh",
        },
        config.AI_WEATHER_CACHE_TTL,
    )
    return _render_hourly(loc, data, count)


async def _query_daily(loc: Location, days: Any) -> str:
    count = _int_arg(days, default=3, low=1, high=10, name="days")
    lat, lon = _lat(loc), _lon(loc)
    daily, alerts = await asyncio.gather(
        _get_json(
            f"/weather/v1/daily/{lat}/{lon}",
            {
                "days": str(count),
                "localTime": "true",
                "lang": "zh",
            },
            config.AI_WEATHER_CACHE_TTL,
        ),
        _get_json(
            f"/weatheralert/v1/current/{lat}/{lon}",
            {"localTime": "true", "lang": "zh"},
            config.AI_WEATHER_CACHE_TTL,
        ),
        return_exceptions=True,
    )
    if isinstance(daily, Exception):
        if isinstance(daily, WeatherError):
            raise daily
        raise WeatherError("天气查询失败。") from daily

    body = _render_daily(loc, daily, count)
    if not isinstance(alerts, Exception):
        alert_text = _render_alerts(
            loc, alerts, include_empty=False, include_header=False
        )
        if alert_text:
            body += "\n\n" + alert_text
    else:
        logger.warning(f"天气预警查询失败（忽略）：{alerts}")
    return body


async def _query_alerts(loc: Location) -> str:
    data = await _get_json(
        f"/weatheralert/v1/current/{_lat(loc)}/{_lon(loc)}",
        {"localTime": "true", "lang": "zh"},
        config.AI_WEATHER_CACHE_TTL,
    )
    return _render_alerts(loc, data, include_empty=True)


async def _query_indices(
    loc: Location, days: Any, index_types: Any
) -> str:
    period, display_days = _indices_days(days)
    types = _normalize_index_types(index_types)
    data = await _get_json(
        f"/v7/indices/{period}",
        {
            "type": ",".join(str(item) for item in types),
            "location": f"{_lon(loc)},{_lat(loc)}",
            "lang": "zh",
        },
        config.AI_WEATHER_CACHE_TTL,
    )
    return _render_indices(loc, data, display_days)


async def _query_air(loc: Location) -> str:
    data = await _get_json(
        f"/airquality/v1/current/{_lat(loc)}/{_lon(loc)}",
        {"lang": "zh"},
        config.AI_WEATHER_CACHE_TTL,
    )
    return _render_air(loc, data)


async def _query_air_daily(loc: Location, days: Any) -> str:
    count = _int_arg(days, default=3, low=1, high=3, name="days")
    data = await _get_json(
        f"/airquality/v1/daily/{_lat(loc)}/{_lon(loc)}",
        {"lang": "zh"},
        config.AI_WEATHER_CACHE_TTL,
    )
    return _render_air_daily(loc, data, count)


async def query(
    city: str,
    kind: str = "current",
    adm: str | None = None,
    hours: Any = None,
    days: Any = None,
    index_types: Any = None,
) -> str:
    if not config.AI_WEATHER_ENABLED:
        raise WeatherError("天气查询未启用。")

    normalized = _normalize_kind(kind)
    loc = await resolve_city(city, adm)
    logger.info(f"天气查询：{normalized} {_location_title(loc)}")
    if loc.ambiguous:
        logger.info(
            f"天气城市匹配：{str(city).strip()} -> "
            f"{_location_title(loc)}（有歧义）"
        )

    if normalized == "current":
        body = await _query_current(loc)
    elif normalized == "minutely":
        body = await _query_minutely(loc)
    elif normalized == "hourly":
        body = await _query_hourly(loc, hours)
    elif normalized == "daily":
        body = await _query_daily(loc, days)
    elif normalized == "alerts":
        body = await _query_alerts(loc)
    elif normalized == "indices":
        body = await _query_indices(loc, days, index_types)
    elif normalized == "air":
        body = await _query_air(loc)
    else:
        body = await _query_air_daily(loc, days)

    return f"{body}\n{_ATTRIBUTION}"