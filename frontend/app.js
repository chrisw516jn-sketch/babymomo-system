// --- 肌少症分期：圓餅圖 ---
const pieEl = document.getElementById("pieChart");
const pie = echarts.getInstanceByDom(pieEl) || echarts.init(pieEl);
const order = ["嚴重肌少症", "肌少症", "肌少症前期", "正常"];
const colorMap = {
  "正常": "#5b8def",
  "肌少症前期": "#4682b4",
  "肌少症": "#f08a4b",
  "嚴重肌少症": "#2f9e44",
};
const rawPie = s.sarcopenia_pie || [];
const pieData = order.map((n) => {
  const hit = rawPie.find((d) => d.name === n);
  return {
    name: n,
    value: hit ? hit.value : 0,
    itemStyle: { color: colorMap[n] }
  };
}).filter((d) => d.value > 0);

pie.setOption({
  tooltip: {
    trigger: "item",
    formatter: "{b}：{c} 人（{d}%）"
  },
  legend: {
    bottom: 0,
    icon: "rect",
    itemWidth: 12,
    itemHeight: 8,
    textStyle: { color: "#64748b", fontSize: 12 },
  },
  series: [{
    type: "pie",
    radius: ["0%", "68%"],
    center: ["50%", "46%"],
    data: pieData,
    label: {
      show: true,
      formatter: "{b}\n{c} 人",
      color: "#334155",
      fontSize: 12,
    },
    labelLine: { length: 12, length2: 8 },
    emphasis: { scale: true, scaleSize: 6 },
  }],
}, true);
