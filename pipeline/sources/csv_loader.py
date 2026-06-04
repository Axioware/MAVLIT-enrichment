import pandas as pd


def load_csv_brands(csv_path: str, niche: str) -> list[dict]:
    """
    Load brand names from a pre-downloaded CSV file.
    Expects a 'brand_name' column; all other columns are ignored.

    Returns dicts with keys: name, niche, source, source_url.
    """
    df = pd.read_csv(csv_path, usecols=lambda col: col == "brand_name").dropna()
    return [
        {
            "name": str(row["brand_name"]).strip(),
            "niche": niche,
            "source": "kaggle",
            "source_url": csv_path,
        }
        for _, row in df.iterrows()
        if str(row["brand_name"]).strip()
    ]