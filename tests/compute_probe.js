// Тестовый харнесс: выполняет compute() из сгенерированной страницы в node.
// Аргументы: <путь к stats.html> <period>. Печатает агрегаты одной JSON-строкой.
// Скрипт страницы сохраняется во временный модуль и подключается через require —
// так его код виден отладчику и не требует динамического eval.
const fs = require('fs');
const os = require('os');
const path = require('path');

const [, , pagePath, periodArg] = process.argv;
const html = fs.readFileSync(pagePath, 'utf8');
const script = html.slice(
  html.indexOf('<script>') + '<script>'.length,
  html.lastIndexOf('</script>')
);

// Минимальный DOM: render() только пишет в innerHTML/textContent и вешает onclick
const els = new Map();
globalThis.document = {
  getElementById(id) {
    if (!els.has(id)) els.set(id, { innerHTML: '', textContent: '', onclick: null });
    return els.get(id);
  },
};

const bundle = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'aibar-probe-')), 'page.cjs');
fs.writeFileSync(bundle, script + '\nmodule.exports = { compute, state };\n', 'utf8');
const api = require(bundle);

api.state.period = Number(periodArg);
const c = api.compute();

console.log(JSON.stringify({
  tAct: c.tAct, tIn: c.tIn, tOut: c.tOut, tCache: c.tCache,
  nSess: c.nSess, nSub: c.nSub,
  byProj: c.byProj, byAgent: c.byAgent, byType: c.byType, byModel: c.byModel,
  mtx: c.mtx, subTok: c.subTok, outMap: c.outMap, byDayMap: c.byDayMap,
  sessIds: c.sess.map(s => s.session_id),
  cutKey: c.cutKey, todayKey: c.todayKey,
}));
