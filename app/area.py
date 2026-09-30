"""Decide whether a job's location is in India or open to people in India.

This is a cheap text check done before any model call, so jobs in other
countries never cost a Kev request.
"""
import re

INDIA = re.compile(
    r"\b(india|bharat|bengaluru|bangalore|mumbai|bombay|pune|hyderabad|chennai|madras|gurugram|gurgaon|noida|"
    r"greater noida|delhi|new delhi|ncr|kolkata|calcutta|ahmedabad|jaipur|kochi|cochin|coimbatore|chandigarh|"
    r"mohali|indore|thiruvananthapuram|trivandrum|vadodara|baroda|nagpur|bhubaneswar|visakhapatnam|vizag|"
    r"mysuru|mysore|lucknow|goa|surat|nashik|mangaluru|mangalore|vijayawada|bhopal|dehradun|kanpur|patna|"
    r"ranchi|guwahati|madurai|tiruchirappalli|hubli|belgaum|belagavi|gandhinagar|navi mumbai|thane|faridabad|"
    r"ghaziabad|secunderabad)\b",
    re.I,
)
REMOTE = re.compile(r"\b(remote|anywhere|worldwide|global|work from home|wfh)\b", re.I)
# Words that may sit next to "remote" without restricting it to another country.
NEUTRAL = re.compile(r"\b(remote|anywhere|worldwide|global|work from home|wfh|apac|asia|asia pacific|emea|"
                     r"first|friendly|only|hybrid|office|in|or|and|the|of|based|location|locations|flexible|"
                     r"multiple|various|timezone|time zone|ist|utc|gmt)\b", re.I)


def in_area(location: str) -> bool:
    loc = (location or "").strip()
    if not loc or re.fullmatch(r"\d+\s+locations?", loc, re.I):
        return True  # unknown (Workday shows "3 Locations" for multi-city jobs): let the model judge it
    if INDIA.search(loc):
        return True
    if REMOTE.search(loc):
        # "Remote" or "Remote (Worldwide)" counts; "Remote - US" does not.
        leftover = re.sub(r"[^a-z]+", " ", NEUTRAL.sub(" ", loc.lower())).strip()
        return leftover == ""
    return False
