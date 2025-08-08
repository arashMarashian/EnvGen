import streamlit as st
from streamlit_folium import st_folium
import folium
from folium.plugins import Draw

st.set_page_config(page_title="Interactive Map", layout="wide")

st.title("Interactive Map")
st.sidebar.info("Draw a rectangle on the map to get its coordinates.")

m = folium.Map(location=[0, 0], zoom_start=2)

Draw(
    export=False,
    draw_options={
        "polyline": True,
        "polygon": False,
        "circle": False,
        "rectangle": True,
        "marker": False,
        "circlemarker": False,
    },
    edit_options={"edit": False}
).add_to(m)

# Run and capture all drawings
map_data = st_folium(m, width=700, height=500, returned_objects=["all_drawings"])

if map_data and "all_drawings" in map_data and map_data["all_drawings"]:
    last_shape = map_data["all_drawings"][-1]
    geometry = last_shape["geometry"]
    coords = geometry["coordinates"]

    shape_type = geometry["type"]

    if shape_type == "Polygon":
        # For rectangles (Polygon), extract first ring
        coords = coords[0]

    elif shape_type == "LineString":
        # For polyline, coords are already fine
        pass

    else:
        st.warning(f"Unsupported shape type: {shape_type}")
        coords = []

    if coords:
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]

        bounds = {
            "south_west": [min(lats), min(lons)],
            "north_east": [max(lats), max(lons)],
        }

    st.markdown("### 🧭 Shape Info:")

    if shape_type == "Polygon":
        st.write("**Type**: Rectangle (Polygon)")
        st.write("**Bounds**:")
        st.write(f"South-West: {bounds['south_west']}")
        st.write(f"North-East: {bounds['north_east']}")

    elif shape_type == "LineString":
        st.write("**Type**: Polyline (LineString)")
        st.write(f"**Number of Nodes:** {len(coords)}")
        st.write("**Coordinates:**")
        for i, point in enumerate(coords):
            st.write(f"Point {i+1}: {point}")


