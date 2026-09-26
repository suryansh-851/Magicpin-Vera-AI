"""Per-trigger-kind strategy: what angle to take, which levers, what CTA shape."""

from __future__ import annotations

# cta shapes
YES_STOP = "binary_yes_stop"
OPEN = "open_ended"
SLOTS = "multi_choice_slot"
CONFIRM = "binary_confirm_cancel"
NONE = "none"

_P = {
    # ---------------- knowledge / external
    "research_digest": dict(
        angle="A new research item just landed. Lead with the headline finding + trial size + source citation, "
              "then tie it to THIS merchant's patient/customer segment (use customer_base numbers). Offer to pull "
              "the abstract and draft a shareable patient/customer note.",
        levers="curiosity, specificity (source citation), reciprocity", cta=OPEN,
        placeholder="No specific paper given: use the TRIGGER_DIGEST_ITEM (or first OTHER_DIGEST item) with its source."),
    "regulation_change": dict(
        angle="A compliance change with a deadline. State what changes (numbers), the effective date, what passes/fails, "
              "and offer a concrete checklist so the merchant is audit-ready before the deadline.",
        levers="loss aversion (deadline), specificity, effort externalisation", cta=YES_STOP,
        placeholder="Use the compliance item from the digest with its source; never invent a regulation."),
    "cde_opportunity": dict(
        angle="A professional-education event: title, date/time, credits, fee. Keep it peer-to-peer; offer to "
              "block the calendar / register.", levers="curiosity, low-friction", cta=YES_STOP,
        placeholder="Use the CDE/training digest item."),
    "supply_alert": dict(
        angle="URGENT product recall/supply alert. Name molecule, batch numbers, manufacturer and the risk level exactly "
              "as given; point out how many of the merchant's chronic/repeat customers may be affected (use the "
              "customer_base number as 'up to N', never invent a smaller count). Offer to draft the customer WhatsApp "
              "+ replacement workflow.", levers="urgency, specificity, effort externalisation", cta=YES_STOP,
        placeholder="Use the alert item in the digest."),
    "category_seasonal": dict(
        angle="Seasonal demand shift for this category: quote the exact trend numbers, recommend a concrete shelf/menu/"
              "service action, and offer to create the post/shareable content.",
        levers="specificity, loss aversion (missing the wave)", cta=YES_STOP,
        placeholder="Use SEASONAL_NOW beats and SEARCH_TRENDS."),
    "category_trend_movement": dict(
        angle="Search-trend movement in the category: quote the % and segment; suggest a matching offer from the "
              "merchant's active offers or the catalog.", levers="curiosity, loss aversion", cta=YES_STOP,
        placeholder="Use the strongest SEARCH_TRENDS item."),
    "festival_upcoming": dict(
        angle="Festival ahead: name it, date + days to go. Give a category-correct plan (service+price offer, not "
              "'% off'), and say when to start promoting. If it's far away, frame as 'lock the plan early'.",
        levers="loss aversion (window), effort externalisation", cta=YES_STOP,
        placeholder="No festival named: use SEASONAL_NOW / SEASONAL_NEXT_MONTH beats as the why-now; do NOT name a festival or date."),
    "ipl_match_today": dict(
        angle="Match today: teams, venue, time. Add judgment from category digest/seasonal beats (e.g., weekend matches "
              "underperform weeknights; is_weeknight flag). Leverage the merchant's existing offer; offer to draft "
              "the promo banner/story.", levers="specificity, contrarian judgment, effort externalisation", cta=YES_STOP,
        placeholder="Use seasonal beats only."),
    "competitor_opened": dict(
        angle="A competitor opened nearby: name, distance, their offer, open date (only if given). Compare with the "
              "merchant's own offer/rating/strengths from reviews; propose one defensive move.",
        levers="loss aversion, social proof, curiosity", cta=YES_STOP,
        placeholder="No competitor details: DO NOT name any competitor. Frame via peer benchmarks (CTR/calls vs peers) "
                    "and one concrete move to stand out."),
    # ---------------- performance / internal
    "perf_dip": dict(
        angle="Metric dropped: state metric, % and window, vs baseline. Give one likely cause from signals/reviews and "
              "one fix that takes <10 min. Loss-aversion framing, not blame.",
        levers="loss aversion, specificity, effort externalisation", cta=YES_STOP,
        placeholder="Use the merchant's own delta_7d numbers (the most negative) and peer gap as the dip."),
    "seasonal_perf_dip": dict(
        angle="Dip is EXPECTED seasonally — reassure with the category seasonal beat, then redirect effort to "
              "retention of existing members/customers (use their numbers). Offer a concrete retention program.",
        levers="anxiety pre-emption, reframing, effort externalisation", cta=YES_STOP,
        placeholder="Use delta_7d + seasonal beat."),
    "perf_spike": dict(
        angle="Metric jumped: metric, %, likely driver. Tell them what worked and propose doubling down "
              "(repeat the post / extend the offer) while momentum lasts.",
        levers="positive reinforcement, curiosity", cta=YES_STOP,
        placeholder="Use the merchant's most positive delta_7d number."),
    "milestone_reached": dict(
        angle="Milestone reached or imminent (value now vs milestone). Celebrate briefly, then convert it into an "
              "action (ask happy customers for reviews / post a thank-you).",
        levers="social proof, pride, small ask", cta=YES_STOP,
        placeholder="Use the merchant's strongest above-peer number as the milestone; don't invent review counts."),
    "review_theme_emerged": dict(
        angle="A review theme is emerging: theme, count, trend, the actual quote. Suggest one operational fix + a "
              "polite public reply template.", levers="loss aversion (reputation), specificity", cta=YES_STOP,
        placeholder="Use REVIEW_THEMES if any; if none, frame via rating/review peer benchmarks."),
    "dormant_with_vera": dict(
        angle="Merchant hasn't replied in a while. Don't guilt-trip. Lead with ONE fresh, valuable number about their "
              "business (perf or peer gap) they haven't seen, and a very easy yes.",
        levers="reciprocity, curiosity", cta=YES_STOP,
        placeholder="Use the most striking perf/peer fact."),
    "curious_ask_due": dict(
        angle="Ask the merchant a low-stakes question about their business this week (what service/dish is most "
              "asked-for), offer a specific guess from their data, and promise a concrete deliverable in return.",
        levers="asking the merchant, reciprocity", cta=OPEN, placeholder="Same."),
    "renewal_due": dict(
        angle="Plan renewal: days left, plan, amount (if given). Show what the plan delivered using real numbers "
              "(views/calls/leads) and what is lost if it lapses. One-tap renew.",
        levers="loss aversion, specificity", cta=YES_STOP,
        placeholder="Use subscription days remaining from MERCHANT and 30-day perf."),
    "winback_eligible": dict(
        angle="Subscription expired: days since expiry, perf drop since, lapsed customers added. Frame what they're "
              "losing and offer a simple restart.", levers="loss aversion", cta=YES_STOP,
        placeholder="Use subscription + delta_7d facts."),
    "gbp_unverified": dict(
        angle="Google profile unverified: say what verification unlocks (estimated uplift if given), the path "
              "(postcard/phone), and offer to walk them through it in 5 minutes.",
        levers="loss aversion, effort externalisation", cta=YES_STOP, placeholder="Same."),
    "active_planning_intent": dict(
        angle="The merchant ALREADY asked for this — do not re-qualify. Deliver a concrete draft of the thing they asked "
              "for (structure, pricing anchored on their existing offers/catalog, schedule), then ask for approval "
              "to publish/send.", levers="effort externalisation, momentum", cta=YES_STOP, placeholder="Same."),
    # ---------------- customer-facing
    "recall_due": dict(
        angle="Customer-facing recall reminder from the merchant: name, time since last visit, which service is due, "
              "offer the actual slots given, price from the merchant's active offer. No medical claims.",
        levers="personalisation, specificity, easy booking", cta=SLOTS,
        placeholder="No slots given: invite them to reply with a convenient time matching their preferred slot."),
    "appointment_tomorrow": dict(
        angle="Customer-facing appointment reminder for tomorrow; confirm/reschedule with one reply.",
        levers="clarity", cta=CONFIRM, placeholder="No time given: ask them to confirm their appointment for tomorrow."),
    "customer_lapsed_soft": dict(
        angle="Customer-facing gentle win-back: warm, no guilt, reference their past service/visits, one concrete "
              "reason to come back (active offer), single YES reply.", levers="personalisation, warmth, low commitment",
        cta=YES_STOP, placeholder="Same."),
    "customer_lapsed_hard": dict(
        angle="Customer-facing win-back after a long gap: warm, 'no judgment', link to their past goal/service, a "
              "no-commitment next step (active offer/trial).", levers="no-shame framing, low commitment",
        cta=YES_STOP, placeholder="Same."),
    "trial_followup": dict(
        angle="Customer-facing follow-up after a trial class/visit: reference the trial date, offer the next session "
              "option(s) given, simple reply to book.", levers="momentum, easy booking", cta=SLOTS, placeholder="Same."),
    "chronic_refill_due": dict(
        angle="Customer-facing refill reminder: medicines by name, run-out date, applicable merchant offers (delivery, "
              "senior discount) — do not compute totals you don't have. Reply to dispatch.",
        levers="precision, convenience", cta=CONFIRM, placeholder="No molecules given: remind generically about their "
              "regular refill and offer delivery if the merchant has it."),
    "wedding_package_followup": dict(
        angle="Customer-facing bridal follow-up: days to wedding, what the next step window is, suggested package "
              "from the merchant's offers/catalog, offer to hold a slot on their preferred day.",
        levers="timeline urgency, personalisation", cta=YES_STOP, placeholder="Same."),
}

GENERIC = dict(
    angle="Explain clearly why you are messaging now (the trigger), anchor on the 2 strongest verifiable facts about "
          "this merchant, and propose one concrete next step you will do for them.",
    levers="specificity, effort externalisation", cta=YES_STOP,
    placeholder="Anchor on the merchant's strongest perf/peer fact.")


def playbook(kind: str) -> dict:
    return _P.get(kind, GENERIC)


def template_name(kind: str, scope: str) -> str:
    prefix = "merchant" if scope == "customer" else "vera"
    return f"{prefix}_{kind or 'generic'}_v1"
