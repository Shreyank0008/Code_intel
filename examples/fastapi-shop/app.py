"""FastAPI Shop — a tiny but real storefront: REST API + OpenAPI + a styled UI.
Boots fast in Docker; ideal for the full ingest → revive → pixel-clone flow."""
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="Nimbus Shop", version="1.0")

PRODUCTS = [
    {"id": 1, "name": "Aurora Lamp", "price": 2499, "tag": "Lighting"},
    {"id": 2, "name": "Drift Chair", "price": 8999, "tag": "Furniture"},
    {"id": 3, "name": "Pulse Speaker", "price": 4599, "tag": "Audio"},
    {"id": 4, "name": "Verdi Planter", "price": 1299, "tag": "Home"},
]


@app.get("/", response_class=HTMLResponse)
def home():
    cards = "".join(
        f"""<div style="background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:18px">
              <div style="font-size:12px;color:#0891b2;font-weight:600">{p['tag']}</div>
              <div style="font-size:16px;font-weight:700;margin-top:6px">{p['name']}</div>
              <div style="color:#64748b;margin-top:4px">₹{p['price']:,}</div>
              <button style="margin-top:12px;background:#16a34a;color:#fff;border:none;border-radius:8px;padding:8px 14px;font-weight:600">Add to cart</button>
            </div>""" for p in PRODUCTS)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Nimbus Shop</title></head>
    <body style="margin:0;font-family:system-ui,-apple-system,sans-serif;background:#f8fafc;color:#0f172a">
      <header style="background:#fff;border-bottom:1px solid #e5e7eb;padding:16px 32px;display:flex;align-items:center;gap:14px">
        <div style="width:30px;height:30px;border-radius:8px;background:linear-gradient(135deg,#16a34a,#0891b2)"></div>
        <b style="font-size:18px">Nimbus Shop</b>
        <nav style="margin-left:auto;display:flex;gap:22px;font-size:14px;color:#475569">
          <a>Home</a><a>Products</a><a>Orders</a><a style="color:#16a34a;font-weight:600">Sign in</a>
        </nav>
      </header>
      <section style="padding:36px 32px 8px">
        <h1 style="font-size:28px;margin:0">Featured products</h1>
        <p style="color:#64748b;margin:6px 0 0">Hand-picked for your space.</p>
      </section>
      <main style="padding:20px 32px;display:grid;grid-template-columns:repeat(4,1fr);gap:16px">{cards}</main>
      <footer style="padding:28px 32px;color:#94a3b8;font-size:13px">© Nimbus — a demo storefront.</footer>
    </body></html>"""


@app.get("/api/products")
def list_products():
    return PRODUCTS


@app.get("/api/products/{pid}")
def get_product(pid: int):
    return next((p for p in PRODUCTS if p["id"] == pid), {})


@app.post("/api/products")
def create_product(product: dict):
    return {"created": True, "product": product}


@app.get("/api/orders")
def list_orders():
    return []


@app.post("/api/orders")
def create_order(order: dict):
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"status": "ok"}
