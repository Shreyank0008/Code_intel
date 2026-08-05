// Express Todo — a Node/Express API + static page. Demonstrates JS route
// detection (app.get/post/…) and a simple UI to clone.
const express = require("express");
const path = require("path");
const app = express();
app.use(express.json());
app.use(express.static(path.join(__dirname, "public")));

let todos = [{ id: 1, text: "Try the pixel-clone agent", done: false }];

app.get("/api/todos", (req, res) => res.json(todos));
app.post("/api/todos", (req, res) => {
  const t = { id: todos.length + 1, text: req.body.text || "", done: false };
  todos.push(t);
  res.status(201).json(t);
});
app.put("/api/todos/:id", (req, res) => {
  const t = todos.find((x) => x.id === Number(req.params.id));
  if (t) t.done = !!req.body.done;
  res.json(t || {});
});
app.delete("/api/todos/:id", (req, res) => {
  todos = todos.filter((x) => x.id !== Number(req.params.id));
  res.json({ ok: true });
});
app.get("/api/health", (req, res) => res.json({ status: "ok" }));

app.listen(80, () => console.log("Express Todo on :80"));
