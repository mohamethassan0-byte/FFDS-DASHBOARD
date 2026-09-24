import os
import io
import glob
import zipfile
from datetime import datetime, timedelta

import numpy as np
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from influxdb_client import InfluxDBClient

# ============================================================
# CONFIG
# ============================================================

DASHBOARD_TITLE = "Forest Sentinel"
STATION_LABEL = "MIRI STATION · SARAWAK"

# --- Everything (Node-RED, InfluxDB, this dashboard) now runs on the same
# laptop, so InfluxDB is reached over localhost instead of a network IP. ---
INFLUXDB_URL = "http://103.133.195.1:8086/"
INFLUXDB_TOKEN = "rsRzIXZdVRh_-bJx597jT-ntZIQaUTkHob7-GQs6vlzZ5pgkJH8ph9bzYtCKF0XGMGYbpzw4kKARDeJG76fRIg=="   # <-- paste your InfluxDB API token here
INFLUXDB_ORG = "UPM"
INFLUXDB_BUCKET = "Sensor Live"

# --- These match what Node-RED actually writes / your Flux queries
# actually read ---
NODE_MEASUREMENT = "Node_Live_Data_5mins"
GROUND_CO2_MEASUREMENT = "Ground_Co2_5min"
WEATHER_MEASUREMENT = "Weather_Live_data_5 MINs"
WEATHER_STATION_ID = "9"   # matches your Flux weather query's StationID filter

LOGO_PATH = r"C:\Users\mohamed\Downloads\logo.png"

IMAGE_BASE_PATH = r"C:\Users\mohamed\Downloads\FFDS_latest_snapshots_saved"
CAMERAS = ["62", "63", "64", "65"]
CAMERA_CHANNEL_LABELS = {"62": "CHANNEL 01", "63": "CHANNEL 02", "64": "CHANNEL 03", "65": "CHANNEL 04"}

REFRESH_SECONDS = 60          # dashboard auto-refresh (live cards / camera / trend charts)
TREND_LOOKBACK = "-24h"       # how far back trend charts on the page look
ACTIVE_NODE_WINDOW = "-15m"   # a node counts as "active" if it reported within this window

# --- FWI-specific settings ---
# FWI is a DAILY index (one value/day at noon), so it does NOT need to be
# recomputed on every 60s dashboard refresh. It's cached separately and only
# recalculated once a day (or when the user presses the recalculate button).
FWI_LOOKBACK = "-90d"     # pull up to 90 days of weather history for FWI training
FWI_CACHE_TTL = 86400     # 24 hours
FWI_SITE_LAT = 4.4        # latitude used in the Canadian FWI day-length/drying-factor tables

FWI_LOCATIONS = [
    {"name": "Node 1 (SN1)", "lat": 4.516083, "lon": 114.167278, "color": "blue"},
    {"name": "Node 2 (SN2)", "lat": 4.516944, "lon": 114.168333, "color": "yellow"},
    {"name": "Weather Station (FFDS GW)", "lat": 4.516389, "lon": 114.167778, "color": "red"},
]

# --- Monitoring Network map height (uses the folium map below, same as
# the FWI Risk Map further down the page — no API token needed) ---
MAPBOX_MAP_HEIGHT = 480

# --- Manual siren (Emergency Control) ---
# This UI only fires a confirmation flow. Wire SIREN_TRIGGER_URL to whatever
# actually sounds the alarm (e.g. a Node-RED HTTP-in node, an MQTT bridge,
# a relay controller endpoint, etc.) — see trigger_siren() below.
SIREN_TRIGGER_URL = ""   # e.g. "http://localhost:1880/siren/activate"

TIME_RANGE_OPTIONS = {
    "Last 5 minutes": (timedelta(minutes=5), "-5m"),
    "Last 10 minutes": (timedelta(minutes=10), "-10m"),
    "Last 15 minutes": (timedelta(minutes=15), "-15m"),
    "Last 20 minutes": (timedelta(minutes=20), "-20m"),
    "Last 1 hour": (timedelta(hours=1), "-1h"),
    "Last 5 hours": (timedelta(hours=5), "-5h"),
    "Last 24 hours": (timedelta(hours=24), "-24h"),
    "Last 2 days": (timedelta(days=2), "-2d"),
    "Last 5 days": (timedelta(days=5), "-5d"),
    "Last 1 week": (timedelta(weeks=1), "-1w"),
    "Last 1 month": (timedelta(days=30), "-30d"),
    "Last 6 months": (timedelta(days=182), "-6mo"),
    "Last 1 year": (timedelta(days=365), "-1y"),
    "Continuous (All Time)": (None, "0"),
}
# ============================================================

st.set_page_config(page_title=DASHBOARD_TITLE, layout="wide", page_icon="🌲")

try:
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=REFRESH_SECONDS * 1000, key="refresh")
except ImportError:
    st.warning("Install `streamlit-autorefresh` for auto-refresh: pip install streamlit-autorefresh")

try:
    from streamlit_folium import st_folium
except ImportError:
    st_folium = None

if not INFLUXDB_TOKEN:
    st.error(
        "No InfluxDB API token set. Open this file and paste your token into "
        "`INFLUXDB_TOKEN` near the top before running the dashboard."
    )
    st.stop()

client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
query_api = client.query_api()


# ------------------------------------------------------------
# Styling
# ------------------------------------------------------------

st.markdown("""
<style>
    .block-container { padding-top: 1.2rem; }

    .top-stripe {
        height: 6px;
        background: linear-gradient(90deg, #be185d 0%, #f59e0b 50%, #16a34a 100%);
        border-radius: 6px 6px 0 0;
        margin-bottom: -6px;
    }
    .header-bar {
        background: linear-gradient(135deg, #0f3d24 0%, #14532d 60%, #166534 100%);
        border-radius: 0 0 14px 14px;
        padding: 22px 28px 18px 28px;
        color: white;
        margin-bottom: 22px;
    }
    .header-title { font-size: 1.7rem; font-weight: 800; margin: 0; }
    .header-sub { font-size: 0.85rem; opacity: 0.85; margin-top: 2px; letter-spacing: 0.04em; }

    .pill-row { display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; }
    .pill {
        border-radius: 999px; padding: 7px 16px; font-size: 0.8rem; font-weight: 600;
        display: inline-flex; align-items: center; gap: 6px;
    }
    .pill-online { background: rgba(34,197,94,0.18); color: #bbf7d0; }
    .pill-time { background: rgba(255,255,255,0.12); color: #e5e7eb; }
    .pill-siren { background: #dc2626; color: white; }

    .section-title {
        font-size: 1.05rem; font-weight: 700; color: #14532d;
        margin: 26px 0 12px 0; padding-left: 10px; border-left: 4px solid #16a34a;
    }
    .live-badge {
        background: #dcfce7; color: #15803d; font-size: 0.7rem; font-weight: 700;
        padding: 3px 10px; border-radius: 999px; letter-spacing: 0.04em; float: right;
    }
    .daily-badge {
        background: #fef3c7; color: #92400e; font-size: 0.7rem; font-weight: 700;
        padding: 3px 10px; border-radius: 999px; letter-spacing: 0.04em; float: right;
    }

    .overview-card {
        background: white; border-radius: 14px; padding: 16px 18px;
        box-shadow: 0 1px 4px rgba(0,0,0,0.07); border: 1px solid #f0f0f0;
        text-align: center; margin-bottom: 14px;
    }
    .overview-icon { font-size: 1.4rem; }
    .overview-label {
        font-size: 0.7rem; color: #6b7280; text-transform: uppercase;
        letter-spacing: 0.06em; font-weight: 700; margin: 4px 0;
    }
    .overview-value { font-size: 1.7rem; font-weight: 800; }

    .metric-card {
        background: white; border-radius: 14px; padding: 16px 18px;
        box-shadow: 0 1px 4px rgba(0,0,0,0.07); margin-bottom: 14px;
        border: 1px solid #f0f0f0; text-align: center;
    }
    .metric-icon { font-size: 1.3rem; margin-bottom: 4px; }
    .metric-label {
        font-size: 0.7rem; color: #6b7280; text-transform: uppercase;
        letter-spacing: 0.06em; font-weight: 700; margin-bottom: 4px;
    }
    .metric-value { font-size: 1.9rem; font-weight: 800; color: #111827; line-height: 1.1; }
    .metric-sub { font-size: 0.85rem; font-weight: 600; color: #6b7280; margin-top: -2px; }
    .metric-unit { font-size: 0.82rem; color: #9ca3af; font-weight: 500; }
    .metric-bar { height: 4px; border-radius: 2px; margin-top: 12px; }

    .node-card {
        background: #f8faf8; border-radius: 16px; padding: 16px;
        border: 1px solid #e5e7eb; margin-bottom: 16px;
    }
    .node-card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
    .node-card-title { font-size: 0.95rem; font-weight: 700; color: #14532d; }

    .chart-card {
        background: white; border-radius: 14px; padding: 16px 18px;
        box-shadow: 0 1px 4px rgba(0,0,0,0.07); border: 1px solid #f0f0f0; margin-bottom: 16px;
    }
    .chart-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
    .chart-title { font-size: 0.9rem; font-weight: 700; color: #14532d; }

    .camera-card {
        background: white; border-radius: 14px; overflow: hidden;
        box-shadow: 0 1px 4px rgba(0,0,0,0.07); border: 1px solid #f0f0f0; margin-bottom: 16px;
    }
    .camera-caption {
        padding: 10px 14px; font-size: 0.8rem; font-weight: 600; color: #14532d;
        display: flex; justify-content: space-between; align-items: center;
    }
</style>
""", unsafe_allow_html=True)


def live_badge():
    return '<span class="live-badge">● LIVE</span>'


def daily_badge():
    return '<span class="daily-badge">🗓 DAILY</span>'


def metric_card(icon, label, value, unit="", bar_color="#22c55e", sub_value=None):
    display_value = value if value not in (None, "—") else "—"
    if isinstance(display_value, float):
        display_value = round(display_value, 1)
    sub_html = f'<div class="metric-sub">{sub_value}</div>' if sub_value else ""
    st.markdown(f"""
    <div class="metric-card">
      <div class="metric-icon">{icon}</div>
      <div class="metric-label">{label}</div>
      <div class="metric-value">{display_value}</div>
      {sub_html}
      <div class="metric-unit">{unit}</div>
      <div class="metric-bar" style="background:{bar_color};"></div>
    </div>
    """, unsafe_allow_html=True)


def overview_card(icon, label, value, value_color="#111827"):
    st.markdown(f"""
    <div class="overview-card">
      <div class="overview-icon">{icon}</div>
      <div class="overview-label">{label}</div>
      <div class="overview-value" style="color:{value_color};">{value}</div>
    </div>
    """, unsafe_allow_html=True)


def compass_direction(degrees):
    if degrees is None:
        return None
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    idx = round(float(degrees) / 45) % 8
    return dirs[idx]


@st.cache_data(ttl=20)
def get_latest_image(cam):
    """Reads directly from disk - fast, since capture and dashboard are on the same laptop."""
    cam_folder = os.path.join(IMAGE_BASE_PATH, f"UPM_{cam}_1")
    files = glob.glob(os.path.join(cam_folder, "*.jpg"))
    if not files:
        return None, None
    latest_file = max(files, key=os.path.getmtime)
    mtime = datetime.fromtimestamp(os.path.getmtime(latest_file))
    return latest_file, mtime


# ------------------------------------------------------------
# Threshold-based bar colors (placeholders - swap in real fire-risk
# cutoffs once you have official ones for each parameter)
# ------------------------------------------------------------

def color_temp(v):
    if v is None: return "#9ca3af"
    return "#22c55e" if v < 32 else ("#f59e0b" if v < 36 else "#ef4444")

def color_humidity(v):
    if v is None: return "#9ca3af"
    return "#ef4444" if v < 40 else ("#f59e0b" if v < 50 else "#22c55e")

def color_water_level(v):
    if v is None: return "#9ca3af"
    return "#ef4444" if v < 100 else ("#f59e0b" if v < 250 else "#22c55e")

def color_co2(v):
    if v is None: return "#9ca3af"
    return "#22c55e" if v < 1000 else ("#f59e0b" if v < 2000 else "#ef4444")

def is_critical(color):
    return color == "#ef4444"


# ------------------------------------------------------------
# InfluxDB helpers (live / fast-refreshing data)
# ------------------------------------------------------------

@st.cache_data(ttl=45)
def get_latest_point(measurement, tag_key=None, tag_value=None, lookback="-15m"):
    tag_filter = ""
    if tag_key and tag_value:
        tag_filter = f'|> filter(fn: (r) => r["{tag_key}"] == "{tag_value}")'
    flux = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: {lookback})
      |> filter(fn: (r) => r["_measurement"] == "{measurement}")
      {tag_filter}
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
      |> sort(columns: ["_time"], desc: true)
      |> limit(n: 1)
    '''
    try:
        tables = query_api.query(flux)
        for table in tables:
            for record in table.records:
                return record.values
    except Exception:
        return {}
    return {}


@st.cache_data(ttl=45)
def get_time_series(measurement, tag_key=None, tag_value=None, lookback=TREND_LOOKBACK):
    tag_filter = ""
    if tag_key and tag_value:
        tag_filter = f'|> filter(fn: (r) => r["{tag_key}"] == "{tag_value}")'
    flux = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: {lookback})
      |> filter(fn: (r) => r["_measurement"] == "{measurement}")
      {tag_filter}
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
      |> sort(columns: ["_time"])
    '''
    try:
        tables = query_api.query(flux)
    except Exception:
        return pd.DataFrame()
    rows = []
    for table in tables:
        for record in table.records:
            rows.append(record.values)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "_time" in df.columns:
        df = df.rename(columns={"_time": "time"})
    if "time" in df.columns and not df.empty:
        # InfluxDB stores timestamps in UTC - convert to Malaysia time (UTC+8)
        # so the trend charts show the actual local time, not a UTC-shifted one
        df["time"] = pd.to_datetime(df["time"]).dt.tz_convert("Asia/Kuala_Lumpur")
    return df


def line_chart(df, y_cols, title, height=280, dual_axis=False, line_colors=None, series_labels=None, badge=None):
    st.markdown(f'''
    <div class="chart-card">
      <div class="chart-header">
        <div class="chart-title">{title}</div>
        {badge if badge else live_badge()}
      </div>
    ''', unsafe_allow_html=True)
    if df.empty or not all(col in df.columns for col in y_cols):
        st.info("No data available yet.")
    else:
        fig = go.Figure()
        colors = line_colors or ["#dc2626", "#2563eb", "#f59e0b", "#9333ea"]
        for i, col in enumerate(y_cols):
            yaxis = "y2" if (dual_axis and i == 1) else "y1"
            display_name = series_labels[i] if series_labels else col
            fig.add_trace(go.Scatter(
                x=df["time"], y=df[col], name=display_name,
                line=dict(color=colors[i % len(colors)]), yaxis=yaxis,
            ))
        layout = dict(
            height=height, margin=dict(l=10, r=10, t=10, b=10),
            plot_bgcolor="white", paper_bgcolor="white",
            legend=dict(orientation="h", y=-0.2),
        )
        if dual_axis:
            layout["yaxis2"] = dict(overlaying="y", side="right")
        fig.update_layout(**layout)
        st.plotly_chart(fig, use_container_width=True)
    st.markdown("</div>", unsafe_allow_html=True)


def df_to_csv_bytes(df):
    return df.to_csv(index=False).encode("utf-8")


@st.cache_data(ttl=120)
def build_images_zip(start_dt):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for cam in CAMERAS:
            cam_folder = os.path.join(IMAGE_BASE_PATH, f"UPM_{cam}_1")
            files = glob.glob(os.path.join(cam_folder, "*.jpg"))
            for f in files:
                if start_dt is not None:
                    mtime = datetime.fromtimestamp(os.path.getmtime(f))
                    if mtime < start_dt:
                        continue
                arcname = f"UPM_{cam}_1/{os.path.basename(f)}"
                zf.write(f, arcname=arcname)
    buffer.seek(0)
    return buffer


# ============================================================
# FIRE WEATHER INDEX (FWI) — CANADIAN FWI SYSTEM CORE FUNCTIONS
# ============================================================
# These are pure math, unchanged from the FWI notebook. They operate on the
# SAME weather fields already flowing into InfluxDB via your weather
# function node: Temperature, Humidity, Windspeed, Opticalrainfall.
# No new sensor / measurement is needed for this.

day_lengths = np.array(
    [
        [11.5, 10.5, 9.2, 7.9, 6.8, 6.2, 6.5, 7.4, 8.7, 10, 11.2, 11.8],
        [10.1, 9.6, 9.1, 8.5, 8.1, 7.8, 7.9, 8.3, 8.9, 9.4, 9.9, 10.2],
        12 * [9],
        [7.9, 8.4, 8.9, 9.5, 9.9, 10.2, 10.1, 9.7, 9.1, 8.6, 8.1, 7.8],
        [6.5, 7.5, 9, 12.8, 13.9, 13.9, 12.4, 10.9, 9.4, 8, 7, 6],
    ]
)

drying_factors = np.array(
    [
        [6.4, 5.0, 2.4, 0.4, -1.6, -1.6, -1.6, -1.6, -1.6, 0.9, 3.8, 5.8],
        12 * [1.39],
        [-1.6, -1.6, -1.6, 0.9, 3.8, 5.8, 6.4, 5.0, 2.4, 0.4, -1.6, -1.6],
    ]
)


def _day_length(lat, mth):
    if -30 > lat >= -90:
        dl = day_lengths[0, :]
    elif -15 > lat >= -30:
        dl = day_lengths[1, :]
    elif 15 > lat >= -15:
        return 9
    elif 30 > lat >= 15:
        dl = day_lengths[3, :]
    elif 90 >= lat >= 30:
        dl = day_lengths[4, :]
    else:
        raise ValueError("Invalid lat specified.")
    return dl[mth - 1]


def _drying_factor(lat, mth):
    if -15 > lat >= -90:
        dlf = drying_factors[0, :]
    elif 15 > lat >= -15:
        return 1.39
    elif 90 >= lat >= 15:
        dlf = drying_factors[2, :]
    else:
        raise ValueError("Invalid lat specified.")
    return dlf[mth - 1]


def ffmc(t, p, w, h, ffmc0):
    w = w * 3.6
    mo = (147.2 * (101.0 - ffmc0)) / (59.5 + ffmc0)

    if p > 0.5:
        rf = p - 0.5
        if mo > 150.0:
            mo = (mo + 42.5 * rf * np.exp(-100.0 / (251.0 - mo)) * (1.0 - np.exp(-6.93 / rf))) + (
                0.0015 * (mo - 150.0) ** 2
            ) * np.sqrt(rf)
        else:
            mo = mo + 42.5 * rf * np.exp(-100.0 / (251.0 - mo)) * (1.0 - np.exp(-6.93 / rf))
        if mo > 250.0:
            mo = 250.0

    ed = (
        0.942 * (h ** 0.679)
        + (11.0 * np.exp((h - 100.0) / 10.0))
        + 0.18 * (21.1 - t) * (1.0 - 1.0 / np.exp(0.1150 * h))
    )

    if mo < ed:
        ew = (
            0.618 * (h ** 0.753)
            + (10.0 * np.exp((h - 100.0) / 10.0))
            + 0.18 * (21.1 - t) * (1.0 - 1.0 / np.exp(0.115 * h))
        )
        if mo < ew:
            kl = 0.424 * (1.0 - ((100.0 - h) / 100.0) ** 1.7) + (0.0694 * np.sqrt(w)) * (
                1.0 - ((100.0 - h) / 100.0) ** 8
            )
            kw = kl * (0.581 * np.exp(0.0365 * t))
            m = ew - (ew - mo) / 10.0 ** kw
        else:
            m = mo
    elif mo == ed:
        m = mo
    else:
        kl = 0.424 * (1.0 - (h / 100.0) ** 1.7) + (0.0694 * np.sqrt(w)) * (1.0 - (h / 100.0) ** 8)
        kw = kl * (0.581 * np.exp(0.0365 * t))
        m = ed + (mo - ed) / 10.0 ** kw

    ffmc_val = (59.5 * (250.0 - m)) / (147.2 + m)
    if ffmc_val > 101.0:
        ffmc_val = 101.0
    elif ffmc_val <= 0.0:
        ffmc_val = 0.0
    return ffmc_val


def dmc(t, p, h, mth, lat, dmc0):
    if t < -1.1:
        rk = 0
    else:
        dl = _day_length(lat, mth)
        rk = 1.894 * (t + 1.1) * (100.0 - h) * dl * 0.0001

    if p > 1.5:
        ra = p
        rw = 0.92 * ra - 1.27
        wmi = 20.0 + 280.0 / np.exp(0.023 * dmc0)
        if dmc0 <= 33.0:
            b = 100.0 / (0.5 + 0.3 * dmc0)
        else:
            if dmc0 <= 65.0:
                b = 14.0 - 1.3 * np.log(dmc0)
            else:
                b = 6.2 * np.log(dmc0) - 17.2
        wmr = wmi + (1000 * rw) / (48.77 + b * rw)
        pr = 43.43 * (5.6348 - np.log(wmr - 20.0))
    else:
        pr = dmc0

    if pr < 0.0:
        pr = 0.0
    dmc_val = pr + rk
    if dmc_val < 0:
        dmc_val = 0.0
    return dmc_val


def dc(t, p, mth, lat, dc0):
    fl = _drying_factor(lat, mth)
    if t < -2.8:
        t = -2.8
    pe = 0.36 * (t + 2.8) + fl
    if p > 2.8:
        ra = p
        rw = 0.83 * ra - 1.27
        smi = 800.0 * np.exp(-dc0 / 400.0)
        smr = smi + 3.937 * rw
        dc_val = 400.0 * np.log(800.0 / smr)
    else:
        dc_val = dc0
    dc_val = dc_val + pe
    return max(dc_val, 0.0)


def isi(w, ffmc_val):
    w = w * 3.6
    mo = 147.2 * (101.0 - ffmc_val) / (59.5 + ffmc_val)
    ff = 19.1152 * np.exp(mo * -0.1386) * (1.0 + (mo ** 5.31) / 49300000.0)
    return ff * np.exp(0.05039 * w)


def bui(dmc_val, dc_val):
    result = np.where(
        dmc_val <= 0.4 * dc_val,
        (0.8 * dc_val * dmc_val) / (dmc_val + 0.4 * dc_val),
        dmc_val - (1.0 - 0.8 * dc_val / (dmc_val + 0.4 * dc_val)) * (0.92 + (0.0114 * dmc_val) ** 1.7),
    )
    return np.clip(result, 0, None)


def fwi_calc(isi_val, bui_val):
    result = np.where(
        bui_val <= 80.0,
        0.1 * isi_val * (0.626 * bui_val ** 0.809 + 2.0),
        0.1 * isi_val * (1000.0 / (25.0 + 108.64 / np.exp(0.023 * bui_val))),
    )
    result = np.atleast_1d(result).astype(float)
    mask = result > 1
    result[mask] = np.exp(2.72 * (0.434 * np.log(result[mask])) ** 0.647)
    return result


def fwi_color(value):
    value = float(value)
    if value <= 1:
        return "blue"
    elif value <= 6:
        return "green"
    elif value <= 13:
        return "yellow"
    else:
        return "red"


def fwi_status(value):
    value = float(value)
    if value <= 1:
        return "Low"
    elif value <= 6:
        return "Moderate"
    elif value <= 13:
        return "High"
    else:
        return "Extreme"


@st.cache_data(ttl=FWI_CACHE_TTL)
def get_fwi_weather_history(lookback=FWI_LOOKBACK):
    """Long-range weather pull, separate from the fast-refreshing live cards.
    Uses the SAME weather measurement/fields already flowing in, filtered to
    the same StationID your Flux weather panel uses.
    """
    flux = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: {lookback})
      |> filter(fn: (r) => r["_measurement"] == "{WEATHER_MEASUREMENT}")
      |> filter(fn: (r) => r["StationID"] == "{WEATHER_STATION_ID}")
      |> filter(fn: (r) =>
          r["_field"] == "Temperature" or
          r["_field"] == "Humidity" or
          r["_field"] == "Windspeed" or
          r["_field"] == "Opticalrainfall"
      )
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
      |> sort(columns: ["_time"])
    '''
    try:
        tables = query_api.query(flux)
    except Exception:
        return pd.DataFrame()
    rows = []
    for table in tables:
        for record in table.records:
            rows.append(record.values)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "_time" not in df.columns:
        return pd.DataFrame()
    df = df.rename(columns={"_time": "Timestamp"})
    df["Timestamp"] = pd.to_datetime(df["Timestamp"]).dt.tz_convert("Asia/Kuala_Lumpur")
    return df


@st.cache_data(ttl=FWI_CACHE_TTL)
def compute_fwi_forecast(_cache_bust):
    """Runs the full FWI pipeline: noon extraction -> daily FWI calc ->
    XGBoost next-day forecast. Cached for FWI_CACHE_TTL seconds (24h by
    default) since FWI only changes once a day - NOT tied to the 60s
    live-card refresh.

    `_cache_bust` lets the sidebar "Recalculate FWI" button force a fresh
    run by passing a changing value (e.g. datetime.now()) without changing
    the actual query logic.
    """
    raw = get_fwi_weather_history()

    result = {"ok": False, "reason": "", }

    if raw.empty or len(raw) < 50:
        result["reason"] = "Not enough historical weather data yet to compute FWI (need several weeks of readings)."
        return result

    df = raw.copy()
    df.columns = df.columns.str.strip()
    df = df.sort_values("Timestamp").set_index("Timestamp")

    # Clean negative rainfall glitches
    if "Opticalrainfall" not in df.columns:
        result["reason"] = "Opticalrainfall field missing from weather data."
        return result
    df.loc[df["Opticalrainfall"] < 0, "Opticalrainfall"] = 0

    required_cols = ["Temperature", "Humidity", "Windspeed", "Opticalrainfall"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        result["reason"] = f"Missing required weather field(s): {missing}"
        return result

    # --- NOON EXTRACTION: one row per day, closest to 13:00 local time ---
    target_minutes = 13 * 60
    row_minutes = df.index.hour * 60 + df.index.minute
    df["distance_from_13"] = abs(row_minutes - target_minutes)

    df_daily = df.groupby(df.index.date, group_keys=False).apply(
        lambda day_group: day_group.loc[[day_group["distance_from_13"].idxmin()]]
    ).copy()
    df_daily = df_daily.sort_index()

    # --- 24h rainfall from the cumulative optical rain gauge ---
    df_daily["Rain_24h"] = df_daily["Opticalrainfall"].diff().fillna(0)
    df_daily["Rain_24h"] = df_daily["Rain_24h"].where(df_daily["Rain_24h"] >= 0, 0)

    df_daily = df_daily.drop(columns=["distance_from_13"]).reset_index()
    df_daily["month"] = df_daily["Timestamp"].dt.month

    if len(df_daily) < 12:
        result["reason"] = f"Only {len(df_daily)} daily noon readings found — need more days of history for a reliable forecast."
        return result

    # --- FWI CALCULATION LOOP (day-over-day, carries state forward) ---
    lat = FWI_SITE_LAT
    ffmc_prev, dmc_prev, dc_prev = 82.1, 12.0, 45.0
    records = []

    for i in range(len(df_daily)):
        temp = float(df_daily.loc[i, "Temperature"])
        rh = float(df_daily.loc[i, "Humidity"])
        wind = float(df_daily.loc[i, "Windspeed"])
        rain = float(df_daily.loc[i, "Rain_24h"])
        month = int(df_daily.loc[i, "month"])

        ffmc_new = ffmc(temp, rain, wind, rh, ffmc_prev)
        dmc_new = dmc(temp, rain, rh, month, lat, dmc_prev)
        dc_new = dc(temp, rain, month, lat, dc_prev)

        isi_new = isi(wind, ffmc_new)
        bui_new = bui(dmc_new, dc_new)
        fwi_new = fwi_calc(isi_new, bui_new)[0]

        records.append({
            "Time": df_daily.loc[i, "Timestamp"],
            "Temperature": temp,
            "Humidity": rh,
            "Windspeed": wind,
            "Rain_24h": rain,
            "FFMC": ffmc_new,
            "DMC": dmc_new,
            "DC": dc_new,
            "ISI": isi_new,
            "BUI": bui_new,
            "FWI": fwi_new,
        })

        ffmc_prev, dmc_prev, dc_prev = ffmc_new, dmc_new, dc_new

    df_daily_fwi = pd.DataFrame(records)

    # --- XGBOOST NEXT-DAY FORECAST ---
    try:
        from xgboost import XGBRegressor
        from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
    except ImportError:
        result["reason"] = "xgboost / scikit-learn not installed. Run: pip install xgboost scikit-learn"
        return result

    df_ml = df_daily_fwi.copy()
    features = ["Temperature", "Humidity", "Windspeed", "Rain_24h"]

    for lag in range(1, 6):
        for col in features:
            df_ml[f"{col}_lag_{lag}"] = df_ml[col].shift(lag)

    df_ml["FWI_future"] = df_ml["FWI"].shift(-1)
    df_ml = df_ml.dropna().reset_index(drop=True)

    if len(df_ml) < 10:
        result["reason"] = f"Only {len(df_ml)} usable training rows after building 5-day lag features — need more history (aim for 30+ days)."
        result["daily_fwi"] = df_daily_fwi
        return result

    X = df_ml.drop(columns=["Time", "FWI", "FWI_future"])
    y = df_ml["FWI_future"]

    train_size = max(int(len(df_ml) * 0.8), 1)
    X_train, X_test = X.iloc[:train_size], X.iloc[train_size:]
    y_train, y_test = y.iloc[:train_size], y.iloc[train_size:]

    model = XGBRegressor(n_estimators=100, learning_rate=0.05, max_depth=3, random_state=42)
    model.fit(X_train, y_train)

    metrics = {"rmse": None, "mae": None, "r2": None}
    if len(X_test) > 0:
        y_pred = model.predict(X_test)
        metrics["rmse"] = float(np.sqrt(mean_squared_error(y_test, y_pred)))
        metrics["mae"] = float(mean_absolute_error(y_test, y_pred))
        if len(y_test) > 1:
            metrics["r2"] = float(r2_score(y_test, y_pred))

    latest_input = X.iloc[-1:].copy()
    tomorrow_fwi = float(model.predict(latest_input)[0])

    today_fwi = float(df_daily_fwi["FWI"].iloc[-1])
    today_date = pd.to_datetime(df_daily_fwi["Time"].iloc[-1])
    tomorrow_date = today_date + pd.Timedelta(days=1)

    forecast_df = pd.DataFrame({
        "Date": [today_date, tomorrow_date],
        "FWI": [today_fwi, tomorrow_fwi],
    })
    forecast_df["Risk"] = forecast_df["FWI"].apply(fwi_status)

    result.update({
        "ok": True,
        "daily_fwi": df_daily_fwi,
        "forecast_df": forecast_df,
        "today_fwi": today_fwi,
        "tomorrow_fwi": tomorrow_fwi,
        "today_date": today_date,
        "tomorrow_date": tomorrow_date,
        "metrics": metrics,
        "n_training_rows": len(X_train),
        "n_test_rows": len(X_test),
    })
    return result


def build_fwi_map(today_fwi=None, tomorrow_fwi=None, observed_time=None, predicted_time=None):
    """Builds the monitoring-network map. Works with or without FWI data:
    node/station markers always show; the colored observed/predicted FWI
    risk circles are only added once today_fwi/tomorrow_fwi are available."""
    import folium
    from folium.features import DivIcon

    have_fwi = today_fwi is not None and tomorrow_fwi is not None

    lat = FWI_LOCATIONS[0]["lat"]
    lon = FWI_LOCATIONS[0]["lon"]

    m = folium.Map(
        location=[lat, lon],
        zoom_start=17,
        tiles="https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}",
        attr="Google Hybrid",
    )

    for loc in FWI_LOCATIONS:
        if "Node 1" in loc["name"]:
            label, station = "SN1", "Sensor Node 1"
        elif "Node 2" in loc["name"]:
            label, station = "SN2", "Sensor Node 2"
        else:
            label, station = "WS", "Weather Station"

        if have_fwi:
            fwi_popup_lines = f"""
            <b>Observed FWI:</b> {today_fwi:.2f}<br>
            <b>Predicted FWI:</b> {tomorrow_fwi:.2f}<br>
            <b>Status:</b> {fwi_status(tomorrow_fwi)}
            """
        else:
            fwi_popup_lines = "<b>FWI:</b> not enough history yet"

        folium.Marker(
            location=[loc["lat"], loc["lon"]],
            popup=f"""
            <h4>{station} ({label})</h4>
            <b>Latitude:</b> {loc['lat']:.6f}<br>
            <b>Longitude:</b> {loc['lon']:.6f}<br><br>
            {fwi_popup_lines}
            """,
            tooltip=f"{station} ({label})",
            icon=DivIcon(
                icon_size=(90, 45),
                icon_anchor=(15, 45),
                html=f"""
                <div style="position:absolute; left:0px; top:0px; width:30px; height:30px;
                    background:#2196F3; border-radius:50% 50% 50% 0; transform:rotate(-45deg);
                    border:2px solid white; box-shadow:0 2px 5px rgba(0,0,0,0.4);">
                    <div style="width:10px; height:10px; background:white; border-radius:50%;
                        margin:8px; transform:rotate(45deg);"></div>
                </div>
                <div style="position:absolute; left:38px; top:5px; background:white;
                    border:1px solid black; border-radius:4px; padding:3px 6px; font-size:12px;
                    font-weight:bold; white-space:nowrap;">{label}</div>
                """,
            ),
        ).add_to(m)

    if have_fwi:
        folium.Circle(
            location=[lat, lon], radius=4500, color=fwi_color(today_fwi), weight=5, opacity=0.8,
            fill=True, fill_color=fwi_color(today_fwi), fill_opacity=0.08,
            popup=f"<h4>Observed FWI Buffer</h4><b>Radius:</b> 4.5 km<br><b>Observed Time:</b> {observed_time}<br><b>FWI:</b> {today_fwi:.2f}<br><b>Status:</b> {fwi_status(today_fwi)}",
        ).add_to(m)

        folium.Circle(
            location=[lat, lon], radius=5000, color=fwi_color(tomorrow_fwi), weight=8, opacity=0.9,
            fill=True, fill_color=fwi_color(tomorrow_fwi), fill_opacity=0.05,
            popup=f"<h4>Predicted FWI Buffer</h4><b>Radius:</b> 5.0 km<br><b>Prediction Time:</b> {predicted_time}<br><b>FWI:</b> {tomorrow_fwi:.2f}<br><b>Status:</b> {fwi_status(tomorrow_fwi)}",
        ).add_to(m)

    legend = """
    <div style="position: fixed; bottom: 20px; left: 20px; width: 220px; background: white;
    border: 2px solid black; border-radius: 5px; padding: 10px; font-size: 12px;
    font-family: Arial; z-index: 9999; box-shadow: 3px 3px 10px rgba(0,0,0,0.3);">
    <b>Fire Weather Index</b><br>
    <span style="display:inline-block;width:12px;height:12px;background:blue;margin-right:6px;"></span>Low (0-1)<br>
    <span style="display:inline-block;width:12px;height:12px;background:green;margin-right:6px;"></span>Moderate (1-6)<br>
    <span style="display:inline-block;width:12px;height:12px;background:yellow;margin-right:6px;"></span>High (6-13)<br>
    <span style="display:inline-block;width:12px;height:12px;background:red;margin-right:6px;"></span>Extreme (>13)
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend))
    return m



def fire_risk_gauge_figure(fwi_value):
    """Semicircular LOW / MODERATE / HIGH / EXTREME gauge, matching the
    Fire Risk Indicator card design."""
    status = fwi_status(fwi_value)
    status_colors = {"Low": "#2563eb", "Moderate": "#16a34a", "High": "#eab308", "Extreme": "#ef4444"}
    fig = go.Figure(go.Indicator(
        mode="gauge",
        value=min(max(fwi_value, 0), 20),
        gauge={
            "axis": {"range": [0, 20], "visible": False},
            "bar": {"color": "#111827", "thickness": 0.22},
            "bgcolor": "white",
            "borderwidth": 0,
            "steps": [
                {"range": [0, 1], "color": "#2563eb"},
                {"range": [1, 6], "color": "#16a34a"},
                {"range": [6, 13], "color": "#eab308"},
                {"range": [13, 20], "color": "#ef4444"},
            ],
        },
        domain={"x": [0, 1], "y": [0, 1]},
    ))
    fig.update_layout(height=230, margin=dict(l=20, r=20, t=10, b=0), paper_bgcolor="rgba(0,0,0,0)")
    return fig, status, status_colors[status]


def fire_risk_panel(fwi_value):
    st.markdown('<div class="chart-card"><div class="chart-title">🔥 Fire Risk Indicator</div>', unsafe_allow_html=True)
    fig, status, color = fire_risk_gauge_figure(fwi_value)
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    st.markdown(f'''
      <div style="text-align:center; margin-top:-14px;">
        <div style="font-size:0.72rem; color:#6b7280; text-transform:uppercase; letter-spacing:0.06em; font-weight:700;">Current Status</div>
        <span style="display:inline-block; margin-top:6px; padding:6px 20px; border-radius:999px;
          background:{color}22; color:{color}; font-weight:800; font-size:0.85rem;">{status.upper()}</span>
      </div>
    ''', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)


def trigger_siren():
    """Fires the actual siren. Fill in SIREN_TRIGGER_URL (or swap this body
    for an MQTT publish / GPIO call) once you know how the hardware expects
    to be told to activate."""
    if not SIREN_TRIGGER_URL:
        raise RuntimeError("SIREN_TRIGGER_URL is not configured yet.")
    import requests
    resp = requests.post(SIREN_TRIGGER_URL, json={"action": "activate"}, timeout=5)
    resp.raise_for_status()


def emergency_control_panel():
    st.markdown('''
    <div class="chart-card" style="background:#fef2f2; border:1px solid #fecaca; margin-bottom:10px;">
      <div style="display:flex; align-items:center; gap:12px;">
        <div style="background:#dc2626; color:white; border-radius:10px; padding:8px 12px; font-size:1.1rem;">⚠️</div>
        <div style="flex:1;">
          <div style="font-weight:700; color:#991b1b; font-size:0.9rem;">Manual Siren Trigger</div>
          <div style="font-size:0.75rem; color:#b91c1c;">Requires operator authorisation</div>
        </div>
      </div>
    </div>
    ''', unsafe_allow_html=True)

    if st.button("🚨 Activate", use_container_width=True, type="primary"):
        st.session_state["siren_confirm"] = True

    if st.session_state.get("siren_confirm"):
        st.warning("Confirm siren activation? This sounds the physical alarm at the station.")
        cc1, cc2 = st.columns(2)
        with cc1:
            if st.button("✅ Confirm", use_container_width=True, key="siren_confirm_btn"):
                try:
                    trigger_siren()
                    st.success("Siren activation sent.")
                except Exception as e:
                    st.error(f"Could not activate siren: {e}")
                st.session_state["siren_confirm"] = False
        with cc2:
            if st.button("✖️ Cancel", use_container_width=True, key="siren_cancel_btn"):
                st.session_state["siren_confirm"] = False


def build_fwi_trend_chart(daily_fwi_df, tomorrow_fwi, tomorrow_date, days_back=7):
    """Observed FWI over the last `days_back` days (solid, filled) plus the
    1-day-ahead forecast as a dashed segment — 'FWI Trend — 7 Days + 1-Day
    Forecast'."""
    df = daily_fwi_df.tail(days_back).reset_index(drop=True)
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["Time"], y=df["FWI"], mode="lines+markers", name="Observed FWI",
        line=dict(color="#dc2626", width=2), marker=dict(size=6),
        fill="tozeroy", fillcolor="rgba(220,38,38,0.08)",
    ))
    last_time, last_fwi = df["Time"].iloc[-1], df["FWI"].iloc[-1]
    fig.add_trace(go.Scatter(
        x=[last_time, tomorrow_date], y=[last_fwi, tomorrow_fwi],
        mode="lines+markers", name="Forecast (+1 day)",
        line=dict(color="#7c3aed", width=2, dash="dash"),
        marker=dict(size=[0, 10], symbol="diamond", color="#7c3aed"),
    ))
    fig.update_layout(
        height=320, margin=dict(l=10, r=10, t=30, b=10),
        plot_bgcolor="white", paper_bgcolor="white",
        legend=dict(orientation="h", y=1.15),
        xaxis_title="Date", yaxis_title="FWI",
    )
    return fig


def fwi_trend_info_block(daily_fwi_df, tomorrow_fwi, tomorrow_date, node1_latest, node2_latest):
    today_row = daily_fwi_df.iloc[-1]
    today_date = pd.to_datetime(today_row["Time"])
    ground_rh_1 = node1_latest.get("RHSensor", "—")
    ground_rh_2 = node2_latest.get("RHSensor", "—")
    ground_rh_1 = f"{ground_rh_1:.1f}" if isinstance(ground_rh_1, (int, float)) else ground_rh_1
    ground_rh_2 = f"{ground_rh_2:.1f}" if isinstance(ground_rh_2, (int, float)) else ground_rh_2

    st.markdown(f'''
    <div style="font-size:0.85rem; line-height:2; color:#374151; padding:4px 4px 10px 4px;">
      <div>🟢 <b>Today {today_date.strftime('%Y-%m-%d')}</b> — FWI {today_row['FWI']:.2f}
        ({fwi_status(today_row['FWI']).upper()})</div>
      <div>🟢 <b>Tomorrow {tomorrow_date.strftime('%Y-%m-%d')}</b> — FWI {tomorrow_fwi:.2f}
        ({fwi_status(tomorrow_fwi).upper()})</div>
      <div style="color:#6b7280;">
        FFMC {today_row['FFMC']:.1f} · DMC {today_row['DMC']:.1f} · DC {today_row['DC']:.1f} ·
        ISI {today_row['ISI']:.1f} · BUI {today_row['BUI']:.1f} · persistence +1d
      </div>
      <div style="color:#6b7280;">Ground RH — node 1: {ground_rh_1}% · node 2: {ground_rh_2}%</div>
    </div>
    ''', unsafe_allow_html=True)


def build_alerts_list(node1_latest, node2_latest, weather_latest):
    checks = [
        ("Node 1 Soil Temperature", node1_latest.get("temperature"), color_temp, "°C"),
        ("Node 1 Soil Moisture", node1_latest.get("RHSensor"), color_humidity, "%"),
        ("Node 1 Atmospheric CO₂", node1_latest.get("CO2ppm1"), color_co2, "ppm"),
        ("Node 2 Soil Temperature", node2_latest.get("temperature"), color_temp, "°C"),
        ("Node 2 Soil Moisture", node2_latest.get("RHSensor"), color_humidity, "%"),
        ("Node 2 Atmospheric CO₂", node2_latest.get("CO2ppm1"), color_co2, "ppm"),
        ("Ambient Temperature", weather_latest.get("Temperature"), color_temp, "°C"),
        ("Ambient Humidity", weather_latest.get("Humidity"), color_humidity, "%"),
    ]
    alerts = []
    for label, value, color_fn, unit in checks:
        if value is not None and is_critical(color_fn(value)):
            alerts.append(f"{label} critical: {value} {unit}")
    return alerts


def alerts_panel(alerts):
    badge_color = "#dc2626" if alerts else "#16a34a"
    st.markdown(f'''
    <div class="chart-card">
      <div class="chart-header">
        <div class="chart-title">🔔 Recent Alerts</div>
        <span style="background:{badge_color}; color:white; border-radius:999px; padding:2px 10px;
          font-size:0.75rem; font-weight:700;">{len(alerts)}</span>
      </div>
    ''', unsafe_allow_html=True)
    if not alerts:
        st.markdown('''
        <div style="text-align:center; padding:22px 0;">
          <div style="font-size:1.6rem; color:#16a34a;">✅</div>
          <div style="margin-top:6px; color:#374151;">No active alerts — all systems nominal</div>
        </div>
        ''', unsafe_allow_html=True)
    else:
        for a in alerts:
            st.markdown(f'<div style="padding:6px 0; border-bottom:1px solid #f3f4f6;">⚠️ {a}</div>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)


# ------------------------------------------------------------
# Fetch core live data once (reused across Overview, sections, downloads)
# ------------------------------------------------------------

node1_latest = get_latest_point(NODE_MEASUREMENT, "NodeID", "1")
node2_latest = get_latest_point(NODE_MEASUREMENT, "NodeID", "2")
weather_latest = get_latest_point(WEATHER_MEASUREMENT, "StationID", WEATHER_STATION_ID)

node1_active = bool(get_latest_point(NODE_MEASUREMENT, "NodeID", "1", lookback=ACTIVE_NODE_WINDOW))
node2_active = bool(get_latest_point(NODE_MEASUREMENT, "NodeID", "2", lookback=ACTIVE_NODE_WINDOW))
active_nodes = int(node1_active) + int(node2_active)

alert_count = 0
for v, fn in [
    (node1_latest.get("temperature"), color_temp),
    (node1_latest.get("RHSensor"), color_humidity),
    (node1_latest.get("CO2ppm1"), color_co2),
    (node2_latest.get("temperature"), color_temp),
    (node2_latest.get("RHSensor"), color_humidity),
    (node2_latest.get("CO2ppm1"), color_co2),
    (weather_latest.get("Temperature"), color_temp),
    (weather_latest.get("Humidity"), color_humidity),
]:
    if is_critical(fn(v)):
        alert_count += 1

fwi_cache_key = st.session_state.get("fwi_cache_key", "initial")
fwi_result = compute_fwi_forecast(fwi_cache_key)


# ------------------------------------------------------------
# Header
# ------------------------------------------------------------

st.markdown('<div class="top-stripe"></div>', unsafe_allow_html=True)
try:
    st.image(LOGO_PATH, width=200)
except Exception:
    pass

st.markdown(f'''
<div class="header-bar">
    <div class="header-title">{DASHBOARD_TITLE}</div>
    <div class="header-sub">{STATION_LABEL}</div>
    <div class="pill-row">
      <span class="pill pill-online">🟢 System Online</span>
      <span class="pill pill-time">🕐 Last update: {datetime.now().strftime('%I:%M:%S %p')}</span>
      <span class="pill pill-siren">⚠️ Manual Siren</span>
    </div>
</div>
''', unsafe_allow_html=True)


# ------------------------------------------------------------
# Overview
# ------------------------------------------------------------

st.markdown('<div class="section-title">Overview</div>', unsafe_allow_html=True)
o1, o2, o3, o4 = st.columns(4)
with o1:
    overview_card("🖥️", "Active Nodes", f"{active_nodes}/2", "#16a34a" if active_nodes == 2 else "#f59e0b")
with o2:
    overview_card("🌡️", "Ambient Temp", f"{weather_latest.get('Temperature', '—')} °C")
with o3:
    overview_card("💧", "Ambient Humidity", f"{weather_latest.get('Humidity', '—')} %")
with o4:
    overview_card("🔔", "Active Alerts", alert_count, "#ef4444" if alert_count > 0 else "#16a34a")


# ------------------------------------------------------------
# Monitoring Network (real Mapbox map) + Emergency Control + Fire Risk
# ------------------------------------------------------------

map_col, side_col = st.columns([2, 1])

with map_col:
    st.markdown('<div class="section-title">Monitoring Network</div>', unsafe_allow_html=True)
    st.markdown(f'''
    <div class="chart-card" style="padding:0; overflow:hidden;">
      <div class="chart-header" style="padding:16px 18px 8px 18px;">
        <div class="chart-title">🗺️ Forest Monitoring Network</div>
        {live_badge()}
      </div>
    ''', unsafe_allow_html=True)
    if fwi_result.get("ok"):
        _observed_time = fwi_result["today_date"].replace(hour=13, minute=0, second=0).strftime("%d %B %Y %I:%M %p")
        _predicted_time = fwi_result["tomorrow_date"].replace(hour=13, minute=0, second=0).strftime("%d %B %Y %I:%M %p")
        network_map = build_fwi_map(fwi_result["today_fwi"], fwi_result["tomorrow_fwi"], _observed_time, _predicted_time)
    else:
        # Not enough FWI history yet - still show the plain node/station
        # markers, just without the colored observed/predicted FWI circles.
        network_map = build_fwi_map()
    if st_folium is not None:
        st_folium(network_map, width=None, height=MAPBOX_MAP_HEIGHT, returned_objects=[])
    else:
        st.warning("Install `streamlit-folium` to display the map: pip install streamlit-folium")
    st.markdown('</div>', unsafe_allow_html=True)

with side_col:
    st.markdown('<div class="section-title">Emergency Control</div>', unsafe_allow_html=True)
    emergency_control_panel()

    st.markdown('<div class="section-title">Fire Risk</div>', unsafe_allow_html=True)
    if fwi_result.get("ok"):
        fire_risk_panel(fwi_result["today_fwi"])
    else:
        st.info("Fire risk indicator needs a bit more FWI history to appear.")


# ------------------------------------------------------------
# Forest Camera Snapshots
# ------------------------------------------------------------

st.markdown(
    f'<div class="section-title">Forest Camera Snapshots'
    f'<span class="live-badge">{len(CAMERAS)} CHANNELS</span></div>',
    unsafe_allow_html=True,
)

cam_row1 = st.columns(2)
cam_row2 = st.columns(2)
cam_layout = cam_row1 + cam_row2

for i, cam in enumerate(CAMERAS):
    with cam_layout[i]:
        path, mtime = get_latest_image(cam)
        st.markdown('<div class="camera-card">', unsafe_allow_html=True)
        if path:
            st.image(path, use_container_width=True)
            time_label = mtime.strftime("%H:%M:%S")
        else:
            st.info("No image found")
            time_label = "—"
        st.markdown(
            f'<div class="camera-caption">'
            f'<span>{CAMERA_CHANNEL_LABELS.get(cam, cam)} · UPM {cam}_1</span>'
            f'<span>{time_label}</span></div>',
            unsafe_allow_html=True,
        )
        st.markdown("</div>", unsafe_allow_html=True)


# ------------------------------------------------------------
# Ground Sensor Readings
# ------------------------------------------------------------

st.markdown('<div class="section-title">Ground Sensor Readings</div>', unsafe_allow_html=True)
node_cols = st.columns(2)
for i, (node_id, data) in enumerate([("1", node1_latest), ("2", node2_latest)]):
    with node_cols[i]:
        st.markdown(f'''
        <div class="node-card">
          <div class="node-card-header">
            <div class="node-card-title">🖥️ Sensor Node 0{node_id}</div>
            {live_badge()}
          </div>
        ''', unsafe_allow_html=True)
        temp = data.get("temperature")
        soil_moisture = data.get("RHSensor")
        water_level = data.get("SoilSensor")
        co2 = data.get("CO2ppm1")
        c1, c2 = st.columns(2)
        with c1: metric_card("🌡️", "Soil Temperature", temp, "°C", color_temp(temp))
        with c2: metric_card("💧", "Soil Moisture", soil_moisture, "%", color_humidity(soil_moisture))
        c3, c4 = st.columns(2)
        with c3: metric_card("🌊", "Ground Water Level", water_level, "mm", color_water_level(water_level))
        with c4: metric_card("☁️", "Atmospheric CO₂", co2, "ppm", color_co2(co2))
        st.markdown("</div>", unsafe_allow_html=True)


# ------------------------------------------------------------
# Weather Station
# ------------------------------------------------------------

st.markdown('<div class="section-title">Weather Station</div>', unsafe_allow_html=True)
wind_dir = compass_direction(weather_latest.get("Winddirection"))

w = st.columns(4)
with w[0]: metric_card("🌡️", "Air Temp", weather_latest.get("Temperature", "—"), "°C", color_temp(weather_latest.get("Temperature")))
with w[1]: metric_card("💧", "Air Humidity", weather_latest.get("Humidity", "—"), "%", color_humidity(weather_latest.get("Humidity")))
with w[2]: metric_card("⏲️", "Pressure", weather_latest.get("Pressure", "—"), "kPa", "#6b7280")
with w[3]: metric_card("💨", "Wind", weather_latest.get("Windspeed", "—"), "km/h", "#0ea5e9", sub_value=wind_dir)

w2 = st.columns(4)
with w2[0]: metric_card("☀️", "Light", weather_latest.get("Illuminance", "—"), "lux", "#f59e0b")
with w2[1]: metric_card("🌧️", "Rainfall", weather_latest.get("DailyRain", "—"), "mm", "#0ea5e9")
with w2[2]: metric_card("🌫️", "PM10", weather_latest.get("PM10", "—"), "µg/m³", "#a855f7")
with w2[3]: metric_card("🌫️", "PM2.5", weather_latest.get("PM2_5", "—"), "µg/m³", "#a855f7")

w3 = st.columns(4)
with w3[0]: metric_card("🔊", "Noise Level", weather_latest.get("Noise", "—"), "dB", "#6b7280")


# ------------------------------------------------------------
# Trends & Analytics (live, fast-refreshing)
# ------------------------------------------------------------

st.markdown('<div class="section-title">Trends &amp; Analytics</div>', unsafe_allow_html=True)

t1, t2 = st.columns(2)
with t1:
    df1 = get_time_series(NODE_MEASUREMENT, "NodeID", "1")
    line_chart(
        df1, ["CO2ppm1", "temperature"], "Node 01 — Environmental Trends",
        dual_axis=True, line_colors=["#f59e0b", "#1e3a8a"],
        series_labels=["CO₂ (ppm)", "Soil Temperature (°C)"],
    )
with t2:
    df2 = get_time_series(NODE_MEASUREMENT, "NodeID", "2")
    line_chart(
        df2, ["CO2ppm1", "temperature"], "Node 02 — Environmental Trends",
        dual_axis=True, line_colors=["#f59e0b", "#1e3a8a"],
        series_labels=["CO₂ (ppm)", "Soil Temperature (°C)"],
    )

g1, g2 = st.columns(2)
with g1:
    line_chart(
        df1, ["RangeSensor", "SoilSensor"], "Node 01 — Ground Movement vs Water Level",
        dual_axis=True, line_colors=["#0ea5e9", "#059669"],
    )
with g2:
    line_chart(
        df2, ["RangeSensor", "SoilSensor"], "Node 02 — Ground Movement vs Water Level",
        dual_axis=True, line_colors=["#0ea5e9", "#059669"],
    )

df_weather = get_time_series(WEATHER_MEASUREMENT, "StationID", WEATHER_STATION_ID)
line_chart(
    df_weather, ["Temperature", "Humidity", "Windspeed"],
    "Weather Comparison — Temperature vs Humidity vs Wind Speed",
    height=320, dual_axis=True,
)

line_chart(
    df_weather, ["PM10", "PM2_5"], "Weather — PM10 vs PM2.5 (Air Quality Trend)",
    line_colors=["#7c3aed", "#059669"],
)

df_ground_co2 = get_time_series(GROUND_CO2_MEASUREMENT)
line_chart(df_ground_co2, ["CO2ppm1"], "Soil CO₂ Level")


# ------------------------------------------------------------
# FIRE WEATHER INDEX FORECAST (daily, separately cached from the
# 60s live refresh above)
# ------------------------------------------------------------

st.markdown(
    '<div class="section-title">Fire Weather Index Forecast</div>',
    unsafe_allow_html=True,
)

if not fwi_result.get("ok"):
    st.info(
        f"FWI forecast not available yet: {fwi_result.get('reason', 'unknown reason')}"
    )
else:
    today_fwi = fwi_result["today_fwi"]
    tomorrow_fwi = fwi_result["tomorrow_fwi"]
    today_date = fwi_result["today_date"]
    tomorrow_date = fwi_result["tomorrow_date"]
    metrics = fwi_result["metrics"]
    daily_fwi_df = fwi_result["daily_fwi"]

    # --- FWI Trend — 7 Days + 1-Day Forecast ---
    if not daily_fwi_df.empty:
        st.markdown(f'''
        <div class="chart-card">
          <div class="chart-header">
            <div class="chart-title">📈 FWI Trend — 7 Days + 1-Day Forecast</div>
            {daily_badge()}
          </div>
        ''', unsafe_allow_html=True)
        st.plotly_chart(
            build_fwi_trend_chart(daily_fwi_df, tomorrow_fwi, tomorrow_date),
            use_container_width=True,
        )
        fwi_trend_info_block(daily_fwi_df, tomorrow_fwi, tomorrow_date, node1_latest, node2_latest)
        st.markdown('</div>', unsafe_allow_html=True)

    # --- Recent Alerts ---
    st.markdown('<div class="section-title">Alerts</div>', unsafe_allow_html=True)
    alerts_panel(build_alerts_list(node1_latest, node2_latest, weather_latest))

    observed_time = today_date.replace(hour=13, minute=0, second=0).strftime("%d %B %Y %I:%M %p")
    predicted_time = tomorrow_date.replace(hour=13, minute=0, second=0).strftime("%d %B %Y %I:%M %p")

    fwi_bar_colors = {"blue": "#2563eb", "green": "#22c55e", "yellow": "#eab308", "red": "#ef4444"}

    f1, f2, f3, f4 = st.columns(4)
    with f1:
        metric_card("🔥", f"Today's FWI ({today_date.strftime('%d %b')})", round(today_fwi, 2),
                    fwi_status(today_fwi), bar_color=fwi_bar_colors[fwi_color(today_fwi)])
    with f2:
        metric_card("🔮", f"Tomorrow's FWI ({tomorrow_date.strftime('%d %b')})", round(tomorrow_fwi, 2),
                    fwi_status(tomorrow_fwi), bar_color=fwi_bar_colors[fwi_color(tomorrow_fwi)])
    with f3:
        model_r2 = metrics.get("r2")
        metric_card("🎯", "Model R²", round(model_r2, 3) if model_r2 is not None else "—", "", "#6b7280")
    with f4:
        model_rmse = metrics.get("rmse")
        metric_card("📉", "Model RMSE", round(model_rmse, 3) if model_rmse is not None else "—", "", "#6b7280")

    # Bar chart: today vs tomorrow
    st.markdown(f'''
    <div class="chart-card">
      <div class="chart-header">
        <div class="chart-title">Current and Next-Day FWI Forecast (XGBoost)</div>
        {daily_badge()}
      </div>
    ''', unsafe_allow_html=True)

    bar_color_map = {"blue": "#2563eb", "green": "#22c55e", "yellow": "#eab308", "red": "#ef4444"}
    fig_fwi = go.Figure(go.Bar(
        x=[today_date.strftime("%d-%b"), tomorrow_date.strftime("%d-%b")],
        y=[today_fwi, tomorrow_fwi],
        marker_color=[bar_color_map[fwi_color(today_fwi)], bar_color_map[fwi_color(tomorrow_fwi)]],
        text=[f"{today_fwi:.2f}", f"{tomorrow_fwi:.2f}"],
        textposition="outside",
    ))
    fig_fwi.update_layout(
        height=280, margin=dict(l=10, r=10, t=20, b=10),
        plot_bgcolor="white", paper_bgcolor="white",
        yaxis_title="Fire Weather Index (FWI)",
    )
    st.plotly_chart(fig_fwi, use_container_width=True)
    st.markdown("</div>", unsafe_allow_html=True)

    # Map
    st.markdown(f'''
    <div class="chart-card">
      <div class="chart-header">
        <div class="chart-title">FWI Risk Map — Observed vs Predicted</div>
        {daily_badge()}
      </div>
    ''', unsafe_allow_html=True)

    fwi_map = build_fwi_map(today_fwi, tomorrow_fwi, observed_time, predicted_time)
    if st_folium is not None:
        st_folium(fwi_map, width=None, height=450, returned_objects=[])
    else:
        st.warning("Install `streamlit-folium` to display the FWI map: pip install streamlit-folium")
        st.components.v1.html(fwi_map.get_root().render(), height=450)
    st.markdown("</div>", unsafe_allow_html=True)

    # Historical daily FWI trend (from the cached calculation)
    daily_fwi_df = fwi_result["daily_fwi"]
    if not daily_fwi_df.empty:
        fig_hist = go.Figure(go.Scatter(
            x=daily_fwi_df["Time"], y=daily_fwi_df["FWI"],
            mode="lines+markers", line=dict(color="#ea580c"),
        ))
        fig_hist.update_layout(
            height=260, margin=dict(l=10, r=10, t=10, b=10),
            plot_bgcolor="white", paper_bgcolor="white",
            yaxis_title="FWI",
        )
        st.markdown(f'''
        <div class="chart-card">
          <div class="chart-header">
            <div class="chart-title">Daily FWI History</div>
            {daily_badge()}
          </div>
        ''', unsafe_allow_html=True)
        st.plotly_chart(fig_hist, use_container_width=True)
        st.markdown("</div>", unsafe_allow_html=True)


# ------------------------------------------------------------
# Sidebar - Downloads + FWI controls
# ------------------------------------------------------------

with st.sidebar:
    st.markdown("### 🔥 Fire Weather Index")
    st.caption(f"Recalculated automatically every {FWI_CACHE_TTL // 3600}h, using up to {FWI_LOOKBACK[1:]} of weather history.")
    if st.button("🔄 Recalculate FWI Now", use_container_width=True):
        st.session_state["fwi_cache_key"] = datetime.now().isoformat()
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.markdown("### 📥 Downloads")

    selected_range = st.selectbox(
        "⏱ Time Range",
        list(TIME_RANGE_OPTIONS.keys()),
        index=6,  # defaults to "Last 24 hours"
    )
    time_delta, flux_lookback = TIME_RANGE_OPTIONS[selected_range]
    image_start_dt = None if time_delta is None else datetime.now() - time_delta

    st.caption(f"Applies to everything below · {selected_range}")
    st.divider()

    st.markdown("**📷 RGB IMAGES**")
    if st.button("🔄 Prepare Images ZIP", use_container_width=True):
        with st.spinner("Zipping images..."):
            st.session_state["images_zip"] = build_images_zip(image_start_dt)
            st.session_state["images_zip_range"] = selected_range

    if "images_zip" in st.session_state:
        st.download_button(
            f"⬇️ RGB IMAGES ({st.session_state.get('images_zip_range', '')})",
            data=st.session_state["images_zip"],
            file_name=f"ffds_images_{datetime.now().strftime('%Y%m%d_%H%M')}.zip",
            mime="application/zip",
            use_container_width=True,
        )
    st.caption("Zip contains one folder per camera (UPM_62_1, UPM_63_1, etc.)")

    st.divider()
    st.markdown("**📊 Sensor Data (CSV)**")

    df_node1_dl = get_time_series(NODE_MEASUREMENT, "NodeID", "1", lookback=flux_lookback)
    st.download_button(
        "Node 1 Data",
        data=df_to_csv_bytes(df_node1_dl) if not df_node1_dl.empty else b"",
        file_name="ffds_node1_data.csv", mime="text/csv",
        disabled=df_node1_dl.empty, use_container_width=True,
    )

    df_node2_dl = get_time_series(NODE_MEASUREMENT, "NodeID", "2", lookback=flux_lookback)
    st.download_button(
        "Node 2 Data",
        data=df_to_csv_bytes(df_node2_dl) if not df_node2_dl.empty else b"",
        file_name="ffds_node2_data.csv", mime="text/csv",
        disabled=df_node2_dl.empty, use_container_width=True,
    )

    df_weather_dl = get_time_series(WEATHER_MEASUREMENT, "StationID", WEATHER_STATION_ID, lookback=flux_lookback)
    st.download_button(
        "Weather Data",
        data=df_to_csv_bytes(df_weather_dl) if not df_weather_dl.empty else b"",
        file_name="ffds_weather_data.csv", mime="text/csv",
        disabled=df_weather_dl.empty, use_container_width=True,
    )

    df_co2_dl = get_time_series(GROUND_CO2_MEASUREMENT, lookback=flux_lookback)
    st.download_button(
        "Ground CO2 Data",
        data=df_to_csv_bytes(df_co2_dl) if not df_co2_dl.empty else b"",
        file_name="ffds_ground_co2_data.csv", mime="text/csv",
        disabled=df_co2_dl.empty, use_container_width=True,
    )

    if fwi_result.get("ok"):
        st.divider()
        st.markdown("**🔥 FWI Data**")
        st.download_button(
            "Daily FWI History",
            data=df_to_csv_bytes(fwi_result["daily_fwi"]),
            file_name="ffds_daily_fwi.csv", mime="text/csv",
            use_container_width=True,
        )
