import concurrent.futures
import io
import math
import time

import requests
import streamlit as st
from PIL import Image

st.set_page_config(page_title="山さがし調査ツール", page_icon="🏔️", layout="centered")

# ── デザイン(namiwodasu.comの土地調査レポートと同系統: 地図/測量テーマ) ──
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Shippori+Mincho:wght@600;800&family=Zen+Kaku+Gothic+New:wght@400;500;700&family=JetBrains+Mono:wght@400;600&display=swap');

    :root {
        --moss: #4a6741; --moss-soft: #4a674122; --soil: #a8632f; --water: #3a7ca8;
    }
    html, body, [class*="css"] { font-family: "Zen Kaku Gothic New", sans-serif; }
    .scout-title {
        font-family: "Shippori Mincho", serif; font-weight: 800; font-size: 2rem;
        text-align: center; margin-bottom: 0;
    }
    .scout-eyebrow {
        font-family: "JetBrains Mono", monospace; font-size: 0.75rem; letter-spacing: 0.15em;
        color: var(--moss); text-transform: uppercase; text-align: center;
    }
    .scout-tagline { text-align: center; color: #6b6656; font-size: 0.9rem; margin-bottom: 1.5rem; }
    .fact-card {
        background: rgba(74,103,65,0.06); border: 1px solid rgba(74,103,65,0.25);
        border-radius: 10px; padding: 14px 16px; margin-bottom: 10px;
    }
    .fact-label { font-size: 0.78rem; color: #6b6656; }
    .fact-value { font-family: "JetBrains Mono", monospace; font-weight: 700; font-size: 1.15rem; margin-top: 2px; }
    .fact-detail { font-size: 0.82rem; color: #6b6656; margin-top: 4px; }
    .scout-footer { text-align: center; margin-top: 40px; color: #6b6656; font-size: 0.78rem; font-family: "JetBrains Mono", monospace; }
    </style>
    <div class="scout-eyebrow">2032 山の基地計画 — 現地調査ツール</div>
    <div class="scout-title">🏔️ 山さがし調査ツール</div>
    <div class="scout-tagline">住所を入れると、標高・洪水リスク・接道・日当たり・Starlinkの目安をまとめて調べます</div>
    """,
    unsafe_allow_html=True,
)


# ── データ取得ロジック(すべて無料の公開API・その場で取得。事前ダウンロードデータは使わない) ──

def geocode(address: str):
    r = requests.get(
        "https://msearch.gsi.go.jp/address-search/AddressSearch",
        params={"q": address}, timeout=15,
    )
    data = r.json()
    if not data:
        return None
    lon, lat = data[0]["geometry"]["coordinates"]
    return {"title": data[0]["properties"]["title"], "lat": lat, "lon": lon}


def get_elevation(lat: float, lon: float):
    try:
        r = requests.get(
            "https://cyberjapandata2.gsi.go.jp/general/dem/scripts/getelevation.php",
            params={"lon": lon, "lat": lat, "outtype": "JSON"}, timeout=15,
        )
        data = r.json()
        elev = data.get("elevation")
        return float(elev) if isinstance(elev, (int, float)) else None
    except Exception:
        return None


FLOOD_LEGEND = [
    ((247, 245, 169), "0.5m未満"),
    ((255, 216, 192), "0.5m以上3.0m未満"),
    ((255, 183, 183), "3.0m以上5.0m未満"),
    ((255, 145, 145), "5.0m以上10.0m未満"),
    ((242, 133, 201), "10.0m以上20.0m未満"),
    ((220, 122, 220), "20.0m以上"),
]


def lonlat_to_tile_float(lon, lat, zoom):
    n = 2 ** zoom
    x = (lon + 180) / 360 * n
    lat_rad = math.radians(lat)
    y = (1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * n
    return x, y


def get_flood_risk(lat: float, lon: float):
    zoom = 15
    x, y = lonlat_to_tile_float(lon, lat, zoom)
    xtile, ytile = int(x), int(y)
    url = f"https://disaportaldata.gsi.go.jp/raster/01_flood_l2_shinsuishin_data/{zoom}/{xtile}/{ytile}.png"
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return {"risk_level": "データなし", "detail": "この地点の浸水想定タイルが見つかりませんでした"}
        img = Image.open(io.BytesIO(r.content)).convert("RGBA")
        px = min(255, int((x - xtile) * 256))
        py = min(255, int((y - ytile) * 256))
        r_, g_, b_, a_ = img.getpixel((px, py))
        if a_ == 0:
            return {"risk_level": "想定なし", "detail": "洪水浸水想定区域外と読み取れました"}
        best_label, best_dist = None, float("inf")
        for (lr, lg, lb), label in FLOOD_LEGEND:
            d = math.sqrt((r_ - lr) ** 2 + (g_ - lg) ** 2 + (b_ - lb) ** 2)
            if d < best_dist:
                best_dist, best_label = d, label
        return {"risk_level": best_label, "detail": "想定最大規模の洪水浸水想定区域データより"}
    except Exception:
        return {"risk_level": "判定不可", "detail": "タイル取得または解析に失敗しました"}


HIGHWAY_ASSUMPTION = {
    "motorway": ("非常に広い(高速道路)", "高速道路は直接の接道対象にならないことが多い"),
    "trunk": ("広い(幹線国道クラス)", "幅員は十分な場合が多い"),
    "primary": ("広い(主要地方道クラス)", "幅員は十分な場合が多い"),
    "secondary": ("やや広い(6m前後の想定)", "接道義務(4m以上)を満たす可能性が高い"),
    "tertiary": ("普通(4〜6m程度の想定)", "接道義務を満たす可能性が高いが要確認"),
    "unclassified": ("不明瞭(3〜5m程度の想定)", "幅員が4m未満の可能性もあり要確認"),
    "residential": ("普通(4〜6m程度・生活道路)", "接道義務を満たす可能性が高いが要確認"),
    "service": ("狭い可能性あり(2〜4m程度)", "私道・通路の可能性があり幅員4m未満のリスクあり"),
    "track": ("狭い(3m未満・未舗装)", "建築基準法上の「道路」に該当しない可能性が高い"),
}


OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
OVERPASS_HEADERS = {
    "Accept": "*/*",
    "User-Agent": "yama-land-scout/0.2 (personal use, 2032 yama base project; https://github.com/yasumoco-ai/yama-land-scout)",
}
OVERPASS_BUDGET_SEC = 30  # 1回の調査で、道路データの取得にかける時間の上限


class RoadFetchError(Exception):
    """道路データのサーバーから取得できなかった(理由つき)。キャッシュされない。"""


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_roads(lat: float, lon: float):
    """周辺の道路(way)を返す。取得できなければ RoadFetchError。
    成功した結果だけが24時間キャッシュされる(失敗は保存しない)。"""
    started = time.monotonic()
    errors = []
    for radius in (100, 300, 800):
        query = f"[out:json][timeout:20];way(around:{radius},{lat},{lon})[highway];out tags geom;"
        data = None
        for attempt in range(2):  # 全サーバーを1巡して、だめなら少し待ってもう1巡
            for endpoint in OVERPASS_ENDPOINTS:
                if time.monotonic() - started > OVERPASS_BUDGET_SEC:
                    break
                host = endpoint.split("/")[2]
                try:
                    r = requests.post(endpoint, data={"data": query}, timeout=15, headers=OVERPASS_HEADERS)
                except requests.Timeout:
                    errors.append(f"{host}: 時間切れ")
                    continue
                except requests.RequestException:
                    errors.append(f"{host}: 接続できません")
                    continue
                if r.status_code != 200:
                    errors.append(f"{host}: HTTP {r.status_code}")
                    continue
                try:
                    data = r.json()
                except ValueError:
                    errors.append(f"{host}: 応答を読めません")
                    continue
                break
            if data is not None or time.monotonic() - started > OVERPASS_BUDGET_SEC:
                break
            time.sleep(3)
        if data is None:
            raise RoadFetchError(" / ".join(dict.fromkeys(errors)) or "理由不明")
        elements = data.get("elements", [])
        if elements:
            return elements
    return []  # サーバーは応答したが、半径800m以内に道路がなかった


def road_from_overpass(lat: float, lon: float):
    m_per_lat = 111320
    m_per_lon = 111320 * math.cos(math.radians(lat))
    try:
        elements = fetch_roads(round(lat, 4), round(lon, 4))
    except RoadFetchError as e:
        return {"road_status": "取得できませんでした", "detail": f"道路データのサーバーから取得できませんでした。時間をおいて再試行してください。(理由: {e})"}
    if not elements:
        return {"road_status": "道路データなし", "detail": "OpenStreetMapは応答しましたが、半径800m以内に道路データがありませんでした"}

    px, py = lon * m_per_lon, lat * m_per_lat
    best_dist, best_way = float("inf"), None
    for el in elements:
        geom = el.get("geometry")
        if not geom:
            continue
        for i in range(len(geom) - 1):
            a, b = geom[i], geom[i + 1]
            ax, ay = a["lon"] * m_per_lon, a["lat"] * m_per_lat
            bx, by = b["lon"] * m_per_lon, b["lat"] * m_per_lat
            dx, dy = bx - ax, by - ay
            len_sq = dx * dx + dy * dy
            t = 0 if len_sq == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / len_sq))
            cx, cy = ax + t * dx, ay + t * dy
            d = math.hypot(px - cx, py - cy)
            if d < best_dist:
                best_dist, best_way = d, el

    if not best_way:
        return {"road_status": "判定不可", "detail": "距離計算に失敗しました"}

    highway = best_way.get("tags", {}).get("highway", "unclassified")
    width_guess, note = HIGHWAY_ASSUMPTION.get(highway, ("不明な種別", "種別が判定表にないため要個別確認"))
    return {
        "distance_m": round(best_dist), "road_type": highway,
        "width_estimate": width_guess, "detail": note,
    }


GSI_VT_URL = "https://cyberjapandata.gsi.go.jp/xyz/experimental_bvmap/{z}/{x}/{y}.pbf"
GSI_VT_ZOOM = 16
RD_CTG = {0: "国道", 1: "都道府県道", 2: "市区町村道", 3: "高速自動車国道等", 5: "その他", 6: "不明"}
# 国土地理院「地物等の属性一覧」の rnkWidth(幅員の区分)
RNK_WIDTH = {
    0: "3m未満", 1: "3m以上5.5m未満", 2: "5.5m以上13m未満",
    3: "13m以上19.5m未満", 4: "19.5m以上", 5: "その他", 6: "不明",
}
RNK_WIDTH_NOTE = {
    0: "接道義務(4m以上)に足りない可能性があり、要確認",
    1: "4m以上あるかは、この区分では分かりません。要確認",
    2: "幅員の区分の上では、4m以上の見込みです",
    3: "幅員の区分の上では、4m以上の見込みです",
    4: "幅員の区分の上では、4m以上の見込みです",
}


def _tile_xy(lat: float, lon: float, z: int):
    n = 2 ** z
    x = (lon + 180) / 360 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def _tile_to_lonlat(tx: int, ty: int, px: float, py: float, extent: int, z: int):
    n = 2 ** z
    x, y = tx + px / extent, ty + py / extent
    lon = x / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lon, lat


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_gsi_roads_tile(z: int, x: int, y: int):
    """国土地理院ベクトルタイル1枚から、幅員の区分つきの道路線を返す。
    取得に失敗したら例外(キャッシュされない)。タイルが無い場所は空リスト。"""
    import mapbox_vector_tile

    r = requests.get(GSI_VT_URL.format(z=z, x=x, y=y), timeout=15, headers=OVERPASS_HEADERS)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    layers = mapbox_vector_tile.decode(r.content, default_options={"y_coord_down": True})
    layer = layers.get("road")
    if not layer:
        return []
    extent = layer.get("extent", 4096)
    lines = []
    for f in layer["features"]:
        props = f["properties"]
        if "rnkWidth" not in props:  # 道路中心線(幅員の区分があるもの)だけを使う
            continue
        geom = f["geometry"]
        parts = geom["coordinates"] if geom["type"] == "MultiLineString" else [geom["coordinates"]]
        for part in parts:
            pts = [_tile_to_lonlat(x, y, px, py, extent, z) for px, py in part]
            if len(pts) >= 2:
                lines.append({"pts": pts, "rdCtg": props.get("rdCtg"), "rnkWidth": props.get("rnkWidth"), "width": props.get("Width")})
    return lines


def road_from_gsi(lat: float, lon: float):
    """国土地理院ベクトルタイルで、最寄りの道路を調べる。(結果 or None, 理由メモ)"""
    fx, fy = _tile_xy(lat, lon, GSI_VT_ZOOM)
    cx, cy = int(fx), int(fy)
    coords = [(cx + dx, cy + dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1)]
    lines, failed = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=9) as ex:
        futs = [ex.submit(fetch_gsi_roads_tile, GSI_VT_ZOOM, x, y) for x, y in coords]
        for fut in futs:
            try:
                lines.extend(fut.result())
            except Exception:
                failed += 1
    if failed == len(coords):
        return None, "国土地理院のデータを取得できませんでした"
    m_per_lat = 111320
    m_per_lon = 111320 * math.cos(math.radians(lat))
    px, py = lon * m_per_lon, lat * m_per_lat
    best_dist, best = float("inf"), None
    for ln in lines:
        pts = ln["pts"]
        for i in range(len(pts) - 1):
            ax, ay = pts[i][0] * m_per_lon, pts[i][1] * m_per_lat
            bx, by = pts[i + 1][0] * m_per_lon, pts[i + 1][1] * m_per_lat
            dx, dy = bx - ax, by - ay
            len_sq = dx * dx + dy * dy
            t = 0 if len_sq == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / len_sq))
            d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d < best_dist:
                best_dist, best = d, ln
    if best is None:
        return None, "国土地理院のデータに、周辺の道路(幅員の区分つき)がありませんでした" + ("(一部のタイルは取得できず)" if failed else "")
    rank = best["rnkWidth"]
    width_label = RNK_WIDTH.get(rank, "不明")
    detail = RNK_WIDTH_NOTE.get(rank, "幅員の区分が不明なため、要確認")
    if best.get("width"):
        detail += f"(実幅員の記載: {best['width']}m)"
    detail += "。出典: 国土地理院ベクトルタイル提供実験"
    return {
        "distance_m": round(best_dist), "road_type": RD_CTG.get(best["rdCtg"], "不明"),
        "width_estimate": f"幅員の区分: {width_label}({RD_CTG.get(best['rdCtg'], '種類不明')})", "detail": detail,
    }, ""


def get_road_access(lat: float, lon: float):
    """接道: 国土地理院を先に使い、取れなければOpenStreetMap(Overpass)で調べる。"""
    gsi, gsi_note = road_from_gsi(lat, lon)
    if gsi:
        return gsi
    osm = road_from_overpass(lat, lon)
    if "distance_m" in osm:
        osm["detail"] += "。出典: OpenStreetMap(国土地理院のデータでは見つかりませんでした)"
        return osm
    osm["detail"] = f"{gsi_note}。" + osm.get("detail", "")
    return osm


COMPASS = [(0, "北"), (45, "北東"), (90, "東"), (135, "南東"), (180, "南"), (225, "南西"), (270, "西"), (315, "北西")]
SUN_QUALITY = {
    "南": "良好", "南東": "良好", "南西": "良好",
    "東": "普通", "西": "普通",
    "北東": "やや不利", "北西": "やや不利", "北": "不利",
}


def compass_label(bearing_deg):
    normalized = bearing_deg % 360
    best_label, best_diff = "北", 360
    for deg, label in COMPASS:
        diff = min(abs(normalized - deg), 360 - abs(normalized - deg))
        if diff < best_diff:
            best_diff, best_label = diff, label
    return best_label


def get_sun_exposure(lat: float, lon: float):
    offset_m = 50
    d_lat = offset_m / 111320
    d_lon = offset_m / (111320 * math.cos(math.radians(lat)))
    n = get_elevation(lat + d_lat, lon)
    s = get_elevation(lat - d_lat, lon)
    e = get_elevation(lat, lon + d_lon)
    w = get_elevation(lat, lon - d_lon)
    if None in (n, s, e, w):
        return {"aspect": "判定不可", "detail": "周辺地点の標高データが取得できませんでした"}
    dzdx = (e - w) / (2 * offset_m)
    dzdy = (n - s) / (2 * offset_m)
    slope_deg = math.degrees(math.atan(math.hypot(dzdx, dzdy)))
    if slope_deg < 2:
        return {"aspect": "平坦", "slope_deg": round(slope_deg, 1), "detail": "傾斜2度未満のため平坦地と判定"}
    bearing_deg = math.degrees(math.atan2(-dzdx, -dzdy))
    label = compass_label(bearing_deg)
    return {
        "aspect": f"{label}向き", "slope_deg": round(slope_deg, 1),
        "sun_quality": SUN_QUALITY.get(label, ""),
        "detail": f"周辺{offset_m}m四方の標高差からの簡易推定",
    }


def get_starlink(lat: float, lon: float):
    center = get_elevation(lat, lon)
    if center is None:
        return {"terrain_check": "判定不可", "detail": "標高データ取得に失敗しました"}
    sample_dist = 200
    samples = []
    for i in range(8):
        angle = 2 * math.pi * i / 8
        d_lon = sample_dist * math.sin(angle) / (111320 * math.cos(math.radians(lat)))
        d_lat = sample_dist * math.cos(angle) / 111320
        e = get_elevation(lat + d_lat, lon + d_lon)
        if e is not None:
            samples.append(e)
    if not samples:
        return {"terrain_check": "判定不可", "detail": "周辺標高データ取得に失敗しました"}
    relative_depth = max(samples) - center
    if relative_depth > 100:
        check = "谷地形の可能性が高く、空の一部が遮られる恐れがあります"
    elif relative_depth > 30:
        check = "やや周囲より低く、方角によっては視界が遮られる可能性があります"
    else:
        check = "周辺と同程度かそれ以上の高さで、空が開けている可能性が高いです"
    return {"terrain_check": check, "relative_height_m": round(relative_depth)}


def get_aerial_photo(lat: float, lon: float):
    zoom = 17
    n = 2 ** zoom
    xtile = int((lon + 180) / 360 * n)
    lat_rad = math.radians(lat)
    ytile = int((1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * n)

    canvas = Image.new("RGB", (768, 768), (200, 200, 200))
    fetched = 0
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            x, y = xtile + dx, ytile + dy
            url = f"https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{zoom}/{x}/{y}.jpg"
            try:
                r = requests.get(url, timeout=15)
                if r.status_code == 200:
                    tile = Image.open(io.BytesIO(r.content))
                    canvas.paste(tile, ((dx + 1) * 256, (dy + 1) * 256))
                    fetched += 1
            except Exception:
                pass
    return canvas if fetched > 0 else None


# ── UI ──

address = st.text_input("住所を入力", placeholder="例: 北海道夕張郡由仁町馬追12-1")
go = st.button("🔍 調べる", use_container_width=True)

if go and address:
    with st.spinner("国土地理院・OpenStreetMap等のデータを取得中..."):
        loc = geocode(address)
        if not loc:
            st.error(f"「{address}」に一致する住所が見つかりませんでした。")
        else:
            lat, lon = loc["lat"], loc["lon"]
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
                fut_elev = ex.submit(get_elevation, lat, lon)
                fut_flood = ex.submit(get_flood_risk, lat, lon)
                fut_road = ex.submit(get_road_access, lat, lon)
                fut_sun = ex.submit(get_sun_exposure, lat, lon)
                fut_star = ex.submit(get_starlink, lat, lon)
                fut_photo = ex.submit(get_aerial_photo, lat, lon)

                elev = fut_elev.result()
                flood = fut_flood.result()
                road = fut_road.result()
                sun = fut_sun.result()
                star = fut_star.result()
                photo = fut_photo.result()

            st.success(f"📍 {loc['title']}（{lat:.5f}, {lon:.5f}）")

            if photo:
                st.image(photo, caption="国土地理院 シームレス空中写真（中心点を囲む約900m四方）", use_container_width=True)

            col1, col2 = st.columns(2)
            with col1:
                st.markdown(
                    f'<div class="fact-card"><div class="fact-label">標高</div>'
                    f'<div class="fact-value">{elev:.1f} m</div></div>' if elev is not None else
                    '<div class="fact-card"><div class="fact-label">標高</div><div class="fact-value">取得失敗</div></div>',
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f'<div class="fact-card"><div class="fact-label">洪水浸水リスク</div>'
                    f'<div class="fact-value">{flood["risk_level"]}</div>'
                    f'<div class="fact-detail">{flood["detail"]}</div></div>',
                    unsafe_allow_html=True,
                )
                sun_extra = f'（{sun.get("slope_deg","?")}°・{sun.get("sun_quality","")}）' if sun.get("aspect") not in ("判定不可", "平坦") else ""
                st.markdown(
                    f'<div class="fact-card"><div class="fact-label">日当たり(斜面方位)</div>'
                    f'<div class="fact-value">{sun.get("aspect","?")}{sun_extra}</div>'
                    f'<div class="fact-detail">{sun.get("detail","")}</div></div>',
                    unsafe_allow_html=True,
                )
            with col2:
                if "distance_m" in road:
                    st.markdown(
                        f'<div class="fact-card"><div class="fact-label">最寄り道路までの距離</div>'
                        f'<div class="fact-value">{road["distance_m"]} m</div>'
                        f'<div class="fact-detail">{road["width_estimate"]}・{road["detail"]}</div></div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f'<div class="fact-card"><div class="fact-label">接道状況</div>'
                        f'<div class="fact-value">{road.get("road_status","?")}</div>'
                        f'<div class="fact-detail">{road.get("detail","")}</div></div>',
                        unsafe_allow_html=True,
                    )
                st.markdown(
                    '<div class="fact-card"><div class="fact-label">Starlink</div>'
                    '<div class="fact-value">提供エリア内</div>'
                    f'<div class="fact-detail">{star.get("terrain_check","")}</div></div>',
                    unsafe_allow_html=True,
                )

            st.caption("国土地理院(ベクトルタイル提供実験を含む)・OpenStreetMap(Overpass API。© OpenStreetMap contributors)・国交省ハザードマップポータルの公開データによる簡易評価です。正式な建築可否・接道義務の判定は自治体窓口での確認が別途必要です。山奥の細い道や私道は、道路データに載っていないことがあります。道路が表示されないことは、道がないことを意味しません。地目・都市計画区域・河川距離の判定は今後のバージョンで対応予定です。")

st.markdown('<div class="scout-footer">制作監督　波を出す</div>', unsafe_allow_html=True)
