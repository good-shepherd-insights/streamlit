import streamlit as st
import socket
st.title("r780badk-in-cloudflare")
st.caption("Serving from a Cloudflare Container instance")
st.metric("hostname", socket.gethostname())
