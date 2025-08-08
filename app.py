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
        "polyline": False,
        "polygon": False,
        "circle": False,
        "rectangle": True,
        "marker": False,
        "circlemarker": False,
    },
    edit_options={"edit": False}
).add_to(m)

map_data = st_folium(m, width=700, height=500, returned_objects=["last_drawn"])

if map_data and map_data.get("last_drawn"):
    coords = map_data["last_drawn"]["geometry"]["coordinates"][0]
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    bounds = {
        "south_west": [min(lats), min(lons)],
        "north_east": [max(lats), max(lons)],
    }
    st.write("Selected bounds:")
    st.write(f"South-West: {bounds['south_west']}")
    st.write(f"North-East: {bounds['north_east']}")
