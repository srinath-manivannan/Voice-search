"""The guided call: fixed questions, tapped answers, one think at the end.

`docs/GUIDED.md` is the design and the measurements behind it. The short
version is in the layout of this package:

- `script`     what is asked, as data. No model reads it and none writes it.
- `shelf`      what is really on a shelf: live stock, the price in force.
- `engine`     which question comes next. A pure function of the shelf and
               the answers so far.
- `recommend`  the only place a model is called, once, and checked after.
- `thinker`    that one call.
- `sessions`   who is mid-way through, what they have spent, and the governor
               that stands in front of every paid call.
- `flow`       the five things a page or a phone line can ask for.

Nothing above `recommend` can spend money, which is the point: a caller who
is only wasting our time never gets past the part that is free.
"""
