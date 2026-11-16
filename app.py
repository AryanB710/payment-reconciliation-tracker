"""Entry point: `python app.py` starts the dev server on http://127.0.0.1:5000"""

import os

from tracker import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=int(os.environ.get("PORT", 5000)))
