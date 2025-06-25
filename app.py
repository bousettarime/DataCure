import streamlit as st
import pandas as pd

st.set_page_config(page_title="Test Datacure")
st.title("Test de chargement CSV")

uploaded_file = st.file_uploader("Charge un fichier CSV", type="csv")

if uploaded_file:
    df = pd.read_csv(uploaded_file)
    st.write("Voici les premières lignes :")
    st.dataframe(df.head())
