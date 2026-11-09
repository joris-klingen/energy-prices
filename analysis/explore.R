library(data.table)
library(arrow)
library(ggplot2)
pkgload::load_all("../ggcpb", quiet = TRUE)

# Rscript's default pdf device cannot draw the house font, and routing it through
# showtext breaks the font in every figure saved after the first; a ragg device reads
# the registered font directly, so print() goes there when run non-interactively
if (!interactive()) ragg::agg_png(tempfile(fileext = ".png"))

# Load ----
prices <- setDT(as.data.frame(open_dataset("data/day_ahead", partitioning = NULL)))
prices <- prices[source == "energy-charts" & bidding_zone == "NL"]
prices[, mtu_local := as.POSIXct(format(mtu_start_utc, tz = "Europe/Amsterdam"), tz = "Europe/Amsterdam")]
prices[, `:=`(date = as.IDate(mtu_local), year = year(mtu_local), month = month(mtu_local),
              hour = hour(mtu_local))]

str(prices)
print(prices[, .N, by = resolution])
print(prices[, .(min = min(mtu_start_utc), max = max(mtu_start_utc))])

# Hourly series ----
# Collapse the quarter-hourly period to hourly means so the full history is comparable
hourly <- prices[, .(price = mean(price_eur_mwh)), by = .(date, year, month, hour)]

print(summary(hourly[, price]))
print(hourly[, .(mean = mean(price), sd = sd(price), p05 = quantile(price, .05),
                 p95 = quantile(price, .95), neg_share = mean(price < 0)), by = year])

# Daily prices over time ----
daily <- hourly[, .(price = mean(price), spread = max(price) - min(price)), by = date]
daily[, date := as.Date(date)]
p_daily <- cpb_line(daily, x = date, y = price,
  linewidth = 0.2,
  title = "NL day-ahead price, daily mean",
  ylab  = "euro/MWh",
  style = "english") +
  # ggcpb gives any non-numeric x a discrete scale, which cannot map dates
  scale_x_date(date_breaks = "2 years", date_labels = "%Y", expand = c(0, 0))
print(p_daily)
save_cpb("analysis/fig_daily.png", p_daily, page = "full")

# Intraday shape by year ----
# A handful of years keeps the series within the house palette
shape <- hourly[, .(price = mean(price)), by = .(year, hour)]
shape[, rel := price / mean(price), by = year]
shape_sel <- shape[year %in% c(2016, 2020, 2023, 2026)]
shape_sel[, year := factor(year)]
p_shape <- cpb_line(shape_sel, x = hour, y = rel, colour = year,
  points = TRUE, palette = "blues",
  title = "Intraday price profile",
  ylab  = "price relative to yearly mean",
  style = "english") +
  scale_x_continuous(breaks = seq(0, 23, 3))
print(p_shape)
save_cpb("analysis/fig_shape.png", p_shape, page = "half")

# Negative prices ----
neg <- hourly[, .(neg_hours = sum(price < 0)), by = year]
p_neg <- cpb_col(neg, x = year, y = neg_hours,
  title = "Hours with a negative price",
  ylab  = "hours per year",
  style = "english") +
  scale_x_continuous(breaks = seq(2015, 2026, 2))
print(p_neg)
save_cpb("analysis/fig_negative.png", p_neg, page = "half")
