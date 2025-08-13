# app.py
# pip install streamlit folium streamlit-folium pillow rasterio matplotlib requests

import base64
import io
import numpy as np
import requests
import streamlit as st
from streamlit_folium import st_folium
import folium
from folium.plugins import Draw
from folium import raster_layers
from PIL import Image
import matplotlib as mpl
from matplotlib import colors
import rasterio
from rasterio.io import MemoryFile

# ---------------- Config ----------------
API_KEY = "23016192f2637c9b8fc6137bcfc852df"   # your key
DEMTYPE = "SRTM15Plus"                         # or GEBCO variant if enabled

st.set_page_config(page_title="EnvGen - Bathymetry Tool (Interactive)", layout="wide")
st.title("Bathymetry Region Selector — Interactive Overlay")

# Cross-version rerun helper
RERUN = getattr(st, "rerun", None) or getattr(st, "experimental_rerun", None)

# ---------------- Session state defaults ----------------
if "roi" not in st.session_state:
    st.session_state.roi = None           # (south, west, north, east)
if "bathy_bytes" not in st.session_state:
    st.session_state.bathy_bytes = None   # cached GeoTIFF bytes for ROI
if "depth_domain_min" not in st.session_state:
    st.session_state.depth_domain_min = -200.0
if "depth_domain_max" not in st.session_state:
    st.session_state.depth_domain_max = 0.0
if "depth_range" not in st.session_state:
    st.session_state.depth_range = (-6.0, 0.0)

# ---------------- Sidebar controls ----------------
with st.sidebar:
    st.subheader("Overlay Controls")

    # Typed domain (min/max)
    colA, colB = st.columns(2)
    with colA:
        dmin_txt = st.text_input("Domain min (m)", value=str(st.session_state.depth_domain_min))
    with colB:
        dmax_txt = st.text_input("Domain max (m)", value=str(st.session_state.depth_domain_max))

    # Parse & validate domain
    try:
        domain_min = float(dmin_txt)
        domain_max = float(dmax_txt)
    except ValueError:
        st.warning("Invalid domain values. Reverting to -200..0 m.")
        domain_min, domain_max = -200.0, 0.0

    if domain_min >= domain_max:
        st.warning("Domain min must be < domain max. Adjusted automatically.")
        if domain_min == domain_max:
            domain_min -= 1.0
        else:
            domain_min, domain_max = min(domain_min, domain_max), max(domain_min, domain_max)

    st.session_state.depth_domain_min = domain_min
    st.session_state.depth_domain_max = domain_max

    # Clamp previous range to the (possibly new) domain
    lo_prev, hi_prev = st.session_state.depth_range
    lo_clamped = max(domain_min, min(hi_prev, max(lo_prev, domain_min)))
    hi_clamped = min(domain_max, max(lo_prev, min(hi_prev, domain_max)))
    if lo_clamped >= hi_clamped:
        lo_clamped, hi_clamped = domain_min, domain_max

    # Range slider within domain
    depth_min, depth_max = st.slider(
        "Depth window (m, negative = depth)",
        min_value=float(domain_min),
        max_value=float(domain_max),
        value=(float(lo_clamped), float(hi_clamped)),
        step=1.0,
        key="depth_slider",
    )
    st.session_state.depth_range = (depth_min, depth_max)

    overlay_opacity = st.slider("Overlay Opacity", 0.0, 1.0, 0.65, 0.05)
    cmap_name = st.selectbox("Colormap", ["viridis", "plasma", "magma", "inferno", "cividis"])
    basemap_choice = st.selectbox("Basemap (initial)", ["Esri.WorldImagery (satellite)", "OpenStreetMap"])
    show_path = st.checkbox("Show route/path", value=True)
    coords_str = st.text_area(
        "Path coordinates (lon,lat,alt triplets)",
        value=(
            "24.96928829283973,60.16299616880952,0 24.99447150950281,60.15586505276418,0 "
            "25.00128796652716,60.15134773226565,0 24.99701757495686,60.14369370333005,0 "
            "24.99270750620464,60.13997506413423,0 24.98002278528934,60.10347534267151,0 "
            "24.9316724702751,59.93452222542251,0 24.75303153087772,59.68715566770889,0 "
            "24.68845717618861,59.61419636957148,0 24.6971006762717,59.59036960367746,0 "
            "24.77312228080529,59.47779918256758,0 24.78083064281022,59.45239569603044,0 "
            "24.77189976385361,59.44489383734989,0"
        ),
        height=120
    )
    map_w = st.number_input("Map width (px)", 400, 1800, 1000, step=50)
    map_h = st.number_input("Map height (px)", 300, 1400, 650, step=50)
    col1, col2 = st.columns(2)
    with col1:
        lock_selection = st.button("Lock/Update Region", type="primary")
    with col2:
        clear_selection = st.button("Clear ROI")

# ---------------- Helpers ----------------
def get_minmax_from_bytes(tif_bytes: bytes):
    """Return (data_min, data_max) from GeoTIFF band 1, ignoring nodata/NaN."""
    with MemoryFile(tif_bytes) as memfile:
        with memfile.open() as src:
            arr = src.read(1).astype(float)
            if src.nodata is not None:
                arr[arr == src.nodata] = np.nan
            # Use oriented array so numbers match what you'll visualize
            left, bottom, right, top = src.bounds
            a = src.transform.a
            e = src.transform.e
            arr_img = arr.copy()
            if e > 0:  # south-up -> flip vertically
                arr_img = np.flipud(arr_img)
            if a < 0:  # x increases to the left -> flip horizontally
                arr_img = np.fliplr(arr_img)
            data_min = float(np.nanmin(arr_img))
            data_max = float(np.nanmax(arr_img))
            return data_min, data_max

def parse_kml_coords(s: str):
    """Parse 'lon,lat,alt lon,lat,alt ...' into arrays of lons/lats."""
    s = s.strip()
    if not s:
        return np.array([]), np.array([])
    lons, lats = [], []
    for triplet in s.split():
        parts = triplet.split(",")
        if len(parts) < 2:
            continue
        lon, lat = float(parts[0]), float(parts[1])
        lons.append(lon); lats.append(lat)
    return np.array(lons), np.array(lats)

def download_bathy_bytes(south, north, west, east, api_key=API_KEY, demtype=DEMTYPE):
    """Download clipped GeoTIFF from OpenTopography globaldem and return bytes."""
    base_url = "https://portal.opentopography.org/API/globaldem"
    params = {
        "demtype": demtype,
        "south": south,
        "north": north,
        "west": west,
        "east": east,
        "outputFormat": "GTiff",
        "API_Key": api_key,
    }
    r = requests.get(base_url, params=params, timeout=300)
    r.raise_for_status()
    return r.content

def read_bathy_image_and_bounds_from_bytes(tif_bytes: bytes):
    """
    Read GeoTIFF -> (arr_img, (south, west, north, east))
    arr_img is oriented so top row = north and left col = west, using the affine transform.
    """
    with MemoryFile(tif_bytes) as memfile:
        with memfile.open() as src:
            arr = src.read(1).astype(float)
            if src.nodata is not None:
                arr[arr == src.nodata] = np.nan

            left, bottom, right, top = src.bounds  # west, south, east, north

            # Orientation from affine transform
            a = src.transform.a     # pixel width (x scale); >0 -> east to right
            e = src.transform.e     # pixel height (y scale); <0 -> north-up

            arr_img = arr.copy()
            if e > 0:      # south-up -> flip vertically so top = north
                arr_img = np.flipud(arr_img)
            if a < 0:      # x increases to the left -> flip horizontally so left = west
                arr_img = np.fliplr(arr_img)

            return arr_img, (bottom, left, top, right)

def make_overlay_data_url(arr_img: np.ndarray, vmin: float, vmax: float, cmap_name: str) -> tuple[str, int]:
    """
    Build a base64 PNG for Leaflet from an already oriented image array.
    Values outside [vmin, vmax] or NaN become transparent.
    """
    data = arr_img.astype(float).copy()
    mask = ~np.isfinite(data) | (data < vmin) | (data > vmax)
    data[mask] = np.nan

    norm = colors.Normalize(vmin=vmin, vmax=vmax, clip=True)
    cmap = mpl.colormaps.get_cmap(cmap_name).copy()
    cmap.set_bad((0, 0, 0, 0))  # NaNs transparent

    rgba = cmap(norm(np.ma.masked_invalid(data)))      # float 0..1
    rgba_uint8 = (rgba * 255).astype(np.uint8)         # uint8

    # Array already oriented — no flip here.
    buf = io.BytesIO()
    Image.fromarray(rgba_uint8).save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}", int(np.isfinite(data).sum())

# ---------------- Clear ROI ----------------
if clear_selection:
    st.session_state.roi = None
    st.session_state.bathy_bytes = None

# ---------------- Choose / lock ROI ----------------
if st.session_state.roi is None:
    # Let user draw a rectangle
    m_draw = folium.Map(location=[60.0, 24.9], zoom_start=6, control_scale=True)
    Draw(
        export=False,
        draw_options={"rectangle": True, "polyline": False, "polygon": False,
                      "circle": False, "marker": False, "circlemarker": False},
        edit_options={"edit": False}
    ).add_to(m_draw)

    drawn = st_folium(m_draw, width=map_w, height=map_h, returned_objects=["all_drawings"])

    # If rectangle drawn and user confirms, store ROI + fetch data
    if drawn and drawn.get("all_drawings"):
        shape = drawn["all_drawings"][-1]
        if shape["geometry"]["type"] == "Polygon":
            coords = shape["geometry"]["coordinates"][0]
            lons = [c[0] for c in coords]
            lats = [c[1] for c in coords]
            south, west = min(lats), min(lons)
            north, east = max(lats), max(lons)
            st.info(f"Proposed ROI: south={south:.5f}, west={west:.5f}, north={north:.5f}, east={east:.5f}")
            if lock_selection:
                try:
                    with st.spinner("Downloading bathymetry…"):
                        st.session_state.bathy_bytes = download_bathy_bytes(south, north, west, east)
                    st.session_state.roi = (south, west, north, east)

                    # --- NEW: auto-fit domain and set slider to [min, 0] (or [min, max] if no zero) ---
                    try:
                        data_min, data_max = get_minmax_from_bytes(st.session_state.bathy_bytes)

                        # Ensure a valid domain (strictly increasing)
                        if data_min == data_max:
                            data_min -= 1e-6  # tiny span so slider won't break

                        # Update domain to actual data range
                        st.session_state.depth_domain_min = data_min
                        st.session_state.depth_domain_max = data_max

                        # Right side: 0 if inside domain, else data_max
                        right = 0.0 if (data_min <= 0.0 <= data_max) else data_max
                        # Left side: the domain min
                        left = data_min

                        # Write desired slider value into session state before rerun
                        st.session_state.depth_range = (left, right)

                    except Exception as mm_err:
                        # If min/max detection fails, fall back but keep the ROI locked
                        st.warning(f"Could not auto-detect depth range: {mm_err}. Using defaults.")
                        st.session_state.depth_domain_min = -200.0
                        st.session_state.depth_domain_max = 0.0
                        st.session_state.depth_range = (-6.0, 0.0)
                    # --- END NEW ---


                    st.success("Region locked.")
                    if RERUN: RERUN()
                except requests.HTTPError as e:
                    st.error(f"Download failed: HTTP {e.response.status_code} — {e.response.text[:300]}")

else:
    # ROI locked — render overlay every rerun
    south, west, north, east = st.session_state.roi
    st.success(f"ROI locked: south={south:.5f}, west={west:.5f}, north={north:.5f}, east={east:.5f}")

    try:
        # Read oriented array + bounds from cached bytes
        arr_img, (south, west, north, east) = read_bathy_image_and_bounds_from_bytes(st.session_state.bathy_bytes)

        # Build base64 overlay
        depth_min, depth_max = st.session_state.depth_range
        overlay_url, visible = make_overlay_data_url(arr_img, depth_min, depth_max, cmap_name)
        st.caption(f"Pixels within [{depth_min}, {depth_max}] m: {visible}")

        center = [(south + north) / 2.0, (west + east) / 2.0]
        m = folium.Map(location=center, zoom_start=8, control_scale=True)

        # Add both base layers; show chosen one initially
        show_esri = "Esri" in basemap_choice
        folium.TileLayer(
            tiles="OpenStreetMap",
            name="OpenStreetMap",
            overlay=False,
            control=True,
            show=not show_esri
        ).add_to(m)
        folium.TileLayer(
            tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
            attr="Esri World Imagery",
            name="Esri.WorldImagery",
            overlay=False,
            control=True,
            show=show_esri
        ).add_to(m)

        raster_layers.ImageOverlay(
            image=overlay_url,
            bounds=[[south, west], [north, east]],
            opacity=overlay_opacity,
            name=f"Bathymetry {depth_min}..{depth_max} m",
            interactive=True,
            cross_origin=False,
            zindex=3,
        ).add_to(m)

        # Optional path
        if show_path and coords_str.strip():
            px, py = parse_kml_coords(coords_str)
            if px.size and py.size:
                folium.PolyLine(
                    locations=list(zip(py, px)), color="red", weight=3, opacity=0.9, tooltip="Route"
                ).add_to(m)
                for lat, lon in zip(py, px):
                    folium.CircleMarker(location=(lat, lon), radius=2, color="red", fill=True).add_to(m)

        folium.LayerControl(collapsed=False).add_to(m)
        st_folium(m, width=map_w, height=map_h)

    except Exception as e:
        st.error(f"Error rendering overlay: {e}")
        st.info("You can Clear the ROI and pick the region again.")
