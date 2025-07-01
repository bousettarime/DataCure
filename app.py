# datacure_cleaning_app.py

import streamlit as st
import pandas as pd
import os
from openai import OpenAI

# === Configuration de la page ===
st.set_page_config(page_title="Datacure Prototype", layout="wide")
st.title("Datacure - Assistant de nettoyage de données (v0)")

# === Chargement de la clé API OpenAI ===
api_key = st.secrets.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")

if not api_key:
    st.warning("⚠️ Clé API OpenAI manquante. Configurez-la dans .streamlit/secrets.toml ou comme variable d'environnement.")
else:
    client = OpenAI(api_key=api_key)

# === Etape 1 : Upload du fichier CSV ===
uploaded_file = st.file_uploader("Charge un fichier CSV", type="csv")

if uploaded_file:
    try:
        df = pd.read_csv(uploaded_file)
        st.subheader("Voici les premières lignes du fichier :")
        st.dataframe(df.head())
    except Exception as e:
        st.error(f"Erreur de lecture du fichier CSV : {e}")
        st.stop()

    # === Etape 2 : Saisie de la commande en langage naturel ===
    user_input = st.text_input(
        "Que veux-tu faire avec ce fichier ?",
        placeholder="Ex : Supprime les lignes où la colonne 'age' est manquante"
    )

    # === Etape 3 : Appel à l'API OpenAI ===
    if user_input and api_key:
        prompt = f"""
Tu es un assistant Python expert en nettoyage de données avec pandas.
Voici un DataFrame nommé df.
L'utilisateur demande : \"{user_input}\"
Retourne uniquement le code Python (sans commentaires ni texte explicatif) pour effectuer cette opération.
        """.strip()

        st.write("🔍 Prompt envoyé à GPT :")
        st.code(prompt)

        with st.spinner("🧠 Génération du code Python par GPT..."):
            try:
                response = client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0
                )

                st.write("📦 Réponse brute de l’API :")
                st.write(response)

                code = response.choices[0].message.content.strip()
                st.subheader("💡 Code généré :")
                st.code(code, language="python")

                if st.button("▶️ Exécuter ce code sur le fichier"):
                    try:
                        local_vars = {"df": df.copy()}
                        exec(code, {}, local_vars)
                        df = local_vars["df"]
                        st.success("✅ Nettoyage appliqué avec succès !")
                        st.dataframe(df.head())
                    except Exception as e:
                        st.error(f"❌ Erreur pendant l'exécution du code : {e}")
            except Exception as e:
                st.error(f"❌ Erreur lors de l'appel à l'API OpenAI : {str(e)}")

    # === Etape 5 : Téléchargement du fichier nettoyé ===
    st.download_button(
        label="📥 Télécharger le fichier nettoyé",
        data=df.to_csv(index=False),
        file_name="fichier_nettoye.csv",
        mime="text/csv"
    )
else:
    st.info("📂 Veuillez charger un fichier CSV pour commencer.")

