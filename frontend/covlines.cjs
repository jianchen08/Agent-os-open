const fs = require("fs")
const dir = process.env.COV_DIR || "coverage"
const txt = fs.readFileSync(dir + "/lcov.info", "utf8")
const targets = process.argv.slice(2)
const B = String.fromCharCode(92)
for (const b of txt.split("end_of_record")) {
  const m = b.match(/SF:(.*)/)
  if (!m) continue
  const f = m[1].split(B).join("/")
  if (!targets.some((t) => f.endsWith(t))) continue
  const lines = b.split(String.fromCharCode(10))
  const lf = lines.find((l) => l.startsWith("LF:"))
  const lh = lines.find((l) => l.startsWith("LH:"))
  const miss = []
  const missFn = []
  for (const l of lines) {
    if (l.startsWith("DA:")) {
      const p = l.slice(3).split(",")
      if (p[1] === "0") miss.push(p[0])
    }
    if (l.startsWith("FNDA:")) {
      const p = l.slice(5).split(",")
      if (p[0] === "0") missFn.push(p[1])
    }
  }
  const short = f.includes("/src/") ? f.split("/src/")[1] : f
  const lfn = lf ? Number(lf.slice(3)) : 0
  const lhn = lh ? Number(lh.slice(3)) : 0
  console.log(short + " L=" + lhn + "/" + lfn + " " + (lfn ? ((lhn / lfn) * 100).toFixed(2) : "?") + "%")
  console.log("  MISS: " + miss.join(","))
  console.log("  MISS_FN: " + missFn.join(" | "))
  console.log("")
}
