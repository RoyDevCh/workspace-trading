// Macro state refresh computation
// This script calculates macro metrics from market data

const cryptoCloses = [
  89387.7578125, 89272.375, 90405.640625, 90640.203125, 92691.7109375,
  92020.9453125, 92511.3359375, 90270.4140625, 90298.7109375, 88175.1796875,
  86419.78125, 87843.984375, 86143.7578125, 85462.5078125, 88103.3828125,
  88344, 88621.75, 88490.015625, 87414, 87611.9609375,
  87234.7421875, 87301.4296875, 87802.15625, 87835.8359375, 87138.140625,
  88430.1328125, 87508.828125, 88731.984375, 89944.6953125, 90603.1875,
  91413.4921875, 93882.5546875, 93729.03125, 91308.0546875, 91027.125,
  90513.1015625, 90386.6484375, 90827.4609375, 91192.9921875, 95321.78125,
  96929.328125, 95551.1875, 95525.1171875, 95099.921875, 93634.4296875,
  92553.59375, 88310.90625, 89376.9609375, 89462.453125, 89503.875,
  89110.734375, 86572.21875, 88267.140625, 89102.5703125, 89184.5703125,
  84561.5859375, 84128.65625, 78621.1171875, 76974.4453125, 78688.765625,
  75633.546875, 73019.703125, 62702.09765625, 70555.390625, 69281.96875,
  70264.7265625, 70120.78125, 68793.9609375, 66991.96875, 66221.84375,
  68857.84375, 69767.625, 68788.1875, 68843.15625, 67494.21875,
  66425.3203125, 66957.5234375, 68005.421875, 68003.765625, 67659.390625,
  64616.73828125, 64080.04296875, 67960.125, 67453.7734375, 65881.796875,
  66995.859375, 65738.1015625, 68775.8515625, 68293.6484375, 72710.578125,
  70841.125, 68136.4921875, 67272.59375, 65969.78125, 68402.3828125,
  69926.921875, 70204.8828125, 70493.4609375, 70968.265625, 71214.625,
  72789.9140625, 74861.0859375, 73922.4765625, 71245.578125, 69912.7890625,
  70522.5859375, 68711.5234375, 67845.2109375, 70914.859375, 70517.859375,
  71309.8828125, 68791.625, 66338.375, 66319.6953125, 65954.921875,
  66691.4453125, 68233.3125, 68078.5546875, 66888.5703125, 66931.1015625
];

const cnCloses = [
  4.608316421508789, 4.620987415313721, 4.694088459014893, 4.599543571472168,
  4.573227405548096, 4.527417182922363, 4.5868730545043945, 4.6014933586120605,
  4.506948947906494, 4.522543430328369, 4.590771675109863, 4.5761518478393555,
  4.593695640563965, 4.703835487365723, 4.680442810058594, 4.738924026489258,
  4.700911045074463, 4.6356072425842285, 4.642430305480957, 4.609291076660156,
  4.615139007568359, 4.683366775512695, 4.673620223999023, 4.685316562652588,
  4.642430305480957, 4.644379138946533, 4.690189361572266, 4.620987415313721,
  4.590771675109863, 4.564455509185791, 4.578100681304932, 4.5576324462890625,
  4.44846773147583, 4.44164514541626, 4.480632305145264, 4.5088982582092285,
  4.50499963760376, 4.517670631408691, 4.568353652954102, 4.547885417938232,
  4.527417182922363, 4.539113521575928, 4.579075336456299, 4.6122145652771,
  4.588822364807129, 4.582974433898926, 4.549835205078125, 4.57517671585083,
  4.5488600730896, 4.503049850463867, 4.580049991607666, 4.549835205078125,
  4.569328784942627, 4.610265254974365, 4.620012283325195, 4.636581897735596,
  4.6453537940979, 4.662898540496826, 4.642430305480957, 4.652176856994629,
  4.632683277130127, 4.72137975692749, 4.79448127746582, 4.776937007904053,
  4.739898681640625, 4.7613420486450195, 4.788633346557617, 4.772063255310059,
  4.742823123931885, 4.7515950202941895, 4.736000061035156, 4.73799991607666,
  4.723999977111816, 4.710999965667725, 4.711999893188477, 4.710000038146973,
  4.724999904632568, 4.76800012588501, 4.710999965667725, 4.599999904632568,
  4.664000034332275, 4.706999778747559, 4.678999900817871, 4.64900016784668,
  4.7270002365112305, 4.732999801635742, 4.7230000495910645, 4.7270002365112305,
  4.671000003814697, 4.724999904632568, 4.75, 4.735000133514404,
  4.724999904632568, 4.738999843597412, 4.677999973297119, 4.610000133514404,
  4.6539998054504395, 4.666999816894531, 4.629000186920166, 4.683000087738037,
  4.710999965667725, 4.693999767303467, 4.677000045776367, 4.679999828338623,
  4.645999908447266, 4.6620001792907715, 4.598999977111816, 4.576000213623047,
  4.429999828338623, 4.479000091552734, 4.544000148773193, 4.48799991607666,
  4.507999897003174, 4.5, 4.4629998207092285, 4.533999919891357,
  4.488999843597412, 4.453999996185303
];

function computeMetrics(closes) {
  const n = closes.length;
  const todayIdx = n - 1;

  // 5-day and 20-day changes
  const change_5 = (closes[todayIdx] / closes[todayIdx - 5]) - 1;
  const change_20 = (closes[todayIdx] / closes[todayIdx - 20]) - 1;

  // Daily returns
  const returns = [];
  for (let i = 1; i < n; i++) {
    returns.push((closes[i] / closes[i-1]) - 1);
  }

  // Realized volatility (full sample, annualized)
  const meanReturn = returns.reduce((a,b) => a+b, 0) / returns.length;
  const variance = returns.reduce((a,b) => a + Math.pow(b - meanReturn, 2), 0) / returns.length;
  const realizedVol = Math.sqrt(variance * 252); // annualized

  // Rolling volatility (20-day, annualized)
  const rollingWindow = 20;
  const recentReturns = returns.slice(-rollingWindow);
  const rollingMean = recentReturns.reduce((a,b) => a+b, 0) / recentReturns.length;
  const rollingVar = recentReturns.reduce((a,b) => a + Math.pow(b - rollingMean, 2), 0) / recentReturns.length;
  const rollingVol = Math.sqrt(rollingVar * 252);

  // Volatility percentile: compare current rolling vol to historical rolling vols
  const historicalRollingVols = [];
  for (let i = rollingWindow; i < returns.length; i++) {
    const window = returns.slice(i - rollingWindow, i);
    const wMean = window.reduce((a,b) => a+b, 0) / window.length;
    const wVar = window.reduce((a,b) => a + Math.pow(b - wMean, 2), 0) / window.length;
    historicalRollingVols.push(Math.sqrt(wVar * 252));
  }
  historicalRollingVols.sort((a,b) => a-b);
  const rank = historicalRollingVols.filter(v => v <= rollingVol).length;
  const volatilityPercentile = rank / historicalRollingVols.length;

  // Stability score: negative if recent vol > historical median?
  const medianVol = historicalRollingVols[Math.floor(historicalRollingVols.length/2)];
  const stabilityScore = rollingVol < medianVol ? 1 : -1;

  return {
    close: closes[todayIdx],
    change_5,
    change_20,
    realized_volatility: realizedVol,
    rolling_volatility: rollingVol,
    volatility_percentile: volatilityPercentile,
    stability_score: stabilityScore,
    count: n
  };
}

const cryptoMetrics = computeMetrics(cryptoCloses);
const cnMetrics = computeMetrics(cnCloses);

console.log("CRYPTO METRICS:", JSON.stringify(cryptoMetrics, null, 2));
console.log("CN EQUITY METRICS:", JSON.stringify(cnMetrics, null, 2));

// Determine preferred market
// Logic: compare recent performance? Or choose lower volatility?
// From notes: "CN equity volatility percentile 0.95 is above the 0.80 threshold."
// High volatility triggers position scaling reduction.
const highVolThreshold = 0.8;
const cnHighVol = cnMetrics.volatility_percentile > highVolThreshold;
const cryptoHighVol = cryptoMetrics.volatility_percentile > highVolThreshold;

// Preferred market: perhaps the one with better recent performance or lower volatility?
// The existing preferred_market was "cn_equity" despite high volatility. Let's check changes.
// Maybe prefer the market with higher 20-day change? Or lower volatility?
// The trading rules emphasize risk control, so likely prefer lower volatility.
let preferredMarket;
if (cnHighVol && cryptoHighVol) {
  // both high vol, maybe pick one with lower percentile
  preferredMarket = cnMetrics.volatility_percentile < cryptoMetrics.volatility_percentile ? "cn_equity" : "crypto";
} else if (!cnHighVol && !cryptoHighVol) {
  // both normal, pick better 20d performance
  preferredMarket = cnMetrics.change_20 > cryptoMetrics.change_20 ? "cn_equity" : "crypto";
} else {
  // pick the one NOT in high vol
  preferredMarket = !cnHighVol ? "cn_equity" : "crypto";
}

// Position scale overrides
const position_scale_overrides = {
  cn_equity: cnHighVol ? 0.5 : 1.0,
  crypto: cryptoHighVol ? 0.5 : 1.0
};

// Risk mode: if both high vol, defensive; if one high vol, maybe cautious; else normal
let risk_mode = "normal";
if (cnHighVol && cryptoHighVol) risk_mode = "defensive";
else if (cnHighVol || cryptoHighVol) risk_mode = "cautious";

console.log("PREFERRED_MARKET:", preferredMarket);
console.log("POSITION_SCALE_OVERRIDES:", JSON.stringify(position_scale_overrides));
console.log("RISK_MODE:", risk_mode);
