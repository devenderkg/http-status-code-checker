import streamlit as st
import requests
import pandas as pd

st.set_page_config(page_title="Bulk URL HTTP Status Code & Redirect Checker | AdFree")

def check_url(url):
    try:
        response = requests.head(url, allow_redirects=True, timeout=5)
        status_code = response.status_code
        final_url = response.url
        return status_code, final_url
    except requests.ConnectionError:
        return "Connection Error", url

def about_section():
    st.sidebar.subheader("About")
    st.sidebar.markdown(
        "Verify the HTTP status codes and redirects for a maximum of 200 URLs to determine their accessibility. Obtain the HTTP status code for each valid URL using this tool for bulk checking. If a URL returns an HTTP status code of 301 or 302, the tool will also capture the redirection URL. This online tool offers a free check for HTTP status codes."
    )

def explore_apps_section():
    st.sidebar.markdown("🌐 **Explore Our Web Apps!**")
    st.sidebar.markdown("Discover more of our handy tools for you:")
    st.sidebar.markdown("[Online Compass](https://myonlinecompass.online/)")
    st.sidebar.markdown("[My Elevation](https://whatismyelevation.pro/)")

def support_section():
    st.sidebar.markdown("🌟 **Support My Work!**")
    st.sidebar.markdown("Hey there, I'm [Devender Gupta](https://twitter.com/devenderkg)! 👋 If you've enjoyed my work, consider supporting my efforts [Buy Me a ☕ Coffee](https://www.buymeacoffee.com/devenderkg).")

def main():
    st.title("Bulk URL HTTP Status Code & Redirect Checker")

    # Additional content in the sidebar
    about_section()
    explore_apps_section()
    support_section()

    # Allow the user to choose input option: Enter URLs or Upload File
    upload_option = st.radio("Choose Input Option:", ["Enter URLs", "Upload File"])

    if upload_option == "Enter URLs":
        # Allow the user to input multiple URLs
        urls = st.text_area("Enter URLs (one per line)", "").splitlines()
    else:
        # Allow the user to upload a file with URLs
        uploaded_file = st.file_uploader("Upload a file with URLs (one per line)", type=["txt", "csv"])

        if uploaded_file is not None:
            df = pd.read_csv(uploaded_file, header=None, names=["URL"])
            urls = df["URL"].tolist()
        else:
            urls = []

    result_data = []

    # Display an initial table with headers
    result_table = st.table(result_data)

    if st.button("Check URLs"):
        st.subheader("Results")

        for i, url in enumerate(urls):
            status_code, final_url = check_url(url)
            result_data.append({
                "URL": url,
                "Status Code": status_code,
                "Final URL": final_url
            })

            # Update the existing table with the latest data
            result_table.table(result_data)

    # Features section
    st.subheader("Features")
    st.markdown("🔄 **Redirect Checker**")
    st.markdown(
        "Check where your website links lead! Easily understand up to ten redirects with detailed visuals. "
        "Explore each step, from the initial URL to the final destination. Track response times for a complete overview "
        "of your website's redirection process."
    )

    st.markdown("🚀 **Easy Input**")
    st.markdown(
        "Check multiple URLs at once! Analyze up to 100 URLs, checking status codes and redirect chains. "
        "The tool defaults to checking HTTP URLs, but you can switch to HTTPS for added security in the settings."
    )

if __name__ == "__main__":
    main()
