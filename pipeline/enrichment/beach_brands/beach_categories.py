"""
pipeline/enrichment/beach_brands/beach_categories.py

Beach / ocean product categories for wikidata_beach_brands.py. The target is
the whole beach and ocean experience, not just beachwear: any brand — from
any primary niche — whose products people use at the beach, surfing,
swimming, diving, exploring the ocean or filming it (Riffe International,
GoPro, DJI, Rip Curl, ...).

Each category is (label, product types / search tags). From these the main
script derives, for every term:
  - Strategy A: the Wikidata product class with that exact English label
    (companies that produce / products made by a company),
  - Strategy C: a Wikidata keyword search restricted to companies / brands,
  - the relevance vocabulary: a flexible regex (`rashguard` = `rash guard` =
    `rash-guard`, `flip-flop` = `flip flops`, plural forms) used to recognise
    the category in descriptions and structured product data.

Add or remove a term here and every strategy follows.
"""

import re

# category -> (label, terms)
CATEGORY_SPECS: dict[str, tuple[str, tuple[str, ...]]] = {
    # --- 1. Ocean and water-sports equipment --------------------------------
    "surfing_equipment": ("Surfing equipment", (
        "surfboard", "shortboard", "longboard surfboard", "soft-top surfboard", "bodyboard", "skimboard",
        "handplane", "surf fins", "surf leash", "surf wax", "traction pad", "surfboard bag", "surfboard repair kit")),
    "freediving_equipment": ("Freediving equipment", (
        "freediving fins", "long-blade fins", "freediving mask", "low-volume mask", "snorkel", "freediving wetsuit",
        "weight belt", "freediving weights", "nose clip", "freediving lanyard")),
    "spearfishing_equipment": ("Spearfishing equipment", (
        "speargun", "polespear", "spearfishing fins", "spearfishing mask", "spearfishing snorkel",
        "spearfishing wetsuit", "dive knife", "float line", "spearfishing float", "float buoy",
        "underwater hunting equipment")),
    "scuba_diving_equipment": ("Scuba diving equipment", (
        "scuba regulator", "dive computer", "buoyancy compensator", "scuba tank", "dive mask", "scuba fins",
        "diving wetsuit", "drysuit", "dive light", "dive torch", "underwater compass", "dive gauge",
        "surface marker buoy")),
    "snorkeling_equipment": ("Snorkeling equipment", (
        "snorkel set", "snorkel mask", "full-face snorkel mask", "snorkel fins", "snorkeling vest", "dry snorkel",
        "underwater viewing equipment", "snorkeling camera mount")),
    "swimming_equipment": ("Swimming equipment", (
        "swim goggles", "swim cap", "swim fins", "kickboard", "pull buoy", "hand paddles", "swim snorkel",
        "open-water swim buoy", "tow float", "swim safety buoy")),
    "paddle_sports_equipment": ("Paddle sports equipment", (
        "stand-up paddleboard", "inflatable sup", "paddleboard paddle", "sup leash", "paddleboard pump", "kayak",
        "kayak paddle", "canoe", "canoe paddle", "kayak seat", "kayak dry bag")),
    "windsurfing_equipment": ("Windsurfing equipment", (
        "windsurfing board", "windsurf sail", "windsurf mast", "windsurf boom", "windsurf harness", "windsurf fin")),
    "kitesurfing_equipment": ("Kitesurfing equipment", (
        "kiteboard", "kitesurfing kite", "kite control bar", "kitesurfing harness", "kiteboarding safety leash",
        "hydrofoil board")),
    "wakeboarding_equipment": ("Wakeboarding equipment", (
        "wakeboard", "wakeboard bindings", "wakesurf board", "tow rope", "wakeboard helmet",
        "wakeboard impact vest")),
    "water_skiing_equipment": ("Water skiing equipment", (
        "water skis", "slalom ski", "tow rope", "water-ski bindings", "towable water sports equipment")),
    "underwater_scooters": ("Underwater scooters", (
        "underwater scooter", "diver propulsion vehicle", "underwater propulsion device",
        "underwater electric scooter")),
    "aquatic_safety_equipment": ("Aquatic safety equipment", (
        "life jacket", "personal flotation device", "rescue buoy", "swim buoy", "marine safety whistle",
        "waterproof emergency beacon", "marine gps locator")),

    # --- 2. Beach apparel, swimwear and protective clothing -----------------
    "surf_apparel": ("Surf apparel", (
        "surfwear", "surf apparel", "boardshorts", "surf trunks", "surf leggings", "surf shirts", "surf jackets",
        "surf poncho", "surf changing robe")),
    "swimwear": ("Swimwear", (
        "swimsuit", "bikini", "one-piece swimsuit", "swim trunks", "swim briefs", "swim shorts", "swim leggings",
        "competitive swimwear", "open-water swimwear")),
    "water_sports_clothing": ("Water-sports clothing", (
        "wetsuit", "drysuit", "rash guard", "rash vest", "neoprene top", "neoprene shorts", "thermal swimwear",
        "water-sports clothing")),
    "uv_protective_clothing": ("UV-protective clothing", (
        "upf shirt", "uv-protection shirt", "sun-protection hoodie", "uv swimwear", "long-sleeve rash guard",
        "sun-protection clothing")),
    "beach_footwear": ("Beach and water-sports footwear", (
        "surf booties", "neoprene boots", "water shoes", "reef shoes", "aqua shoes", "diving boots",
        "beach sandals", "flip-flops", "water-sports footwear")),
    "beach_headwear": ("Beach headwear", (
        "surf hat", "swim cap", "beach hat", "sun hat", "wide-brim sun hat", "uv-protection hat",
        "water-sports cap")),
    "beach_outerwear": ("Beach outerwear", (
        "surf poncho", "changing robe", "beach cover-up", "swim cover-up", "coastal jacket", "windbreaker",
        "waterproof shell")),
    "beach_accessories_apparel": ("Beach apparel accessories", (
        "surf gloves", "neoprene gloves", "wetsuit hood", "rash guard", "swim gloves", "surf belt",
        "waterproof waist pack")),

    # --- 3. Cameras, drones and ocean content creation ----------------------
    "action_cameras": ("Action cameras", (
        "action camera", "waterproof action camera", "sports camera", "wearable camera", "360-degree camera",
        "helmet camera", "surf camera")),
    "underwater_cameras": ("Underwater cameras", (
        "underwater camera", "diving camera", "underwater photography camera", "waterproof digital camera",
        "underwater video camera", "underwater camera housing")),
    "camera_housings": ("Camera housings", (
        "underwater camera housing", "waterproof camera case", "dive camera housing", "action camera dive case",
        "underwater camera dome port")),
    "camera_mounts": ("Camera mounts", (
        "surfboard camera mount", "surfboard camera attachment", "helmet mount", "chest mount", "head mount",
        "dive mask camera mount", "mouth mount", "underwater camera tray")),
    "camera_accessories": ("Camera accessories", (
        "waterproof camera battery", "action camera battery", "camera float", "floating camera grip",
        "waterproof remote", "camera tether", "underwater camera filter", "dive light for camera")),
    "drones": ("Drones", (
        "camera drone", "aerial photography drone", "waterproof drone", "marine drone", "coastal mapping drone",
        "drone camera", "quadcopter")),   # "quadcopter" is how Wikidata labels DJI's products. A bare "drone"
                                          # term is NOT used: it pulls in delivery / inspection / insurance firms.
    "underwater_imaging": ("Underwater imaging", (
        "underwater video light", "underwater photography light", "dive video light", "underwater flash",
        "underwater strobe", "underwater camera monitor")),
    "content_creation_gear": ("Content creation gear", (
        "waterproof microphone", "wireless microphone", "outdoor camera", "portable tripod", "waterproof tripod",
        "action camera stabilizer", "mobile video rig")),
    "marine_imaging_technology": ("Marine imaging technology", (
        "underwater imaging system", "underwater inspection camera", "marine observation camera",
        "remotely operated underwater vehicle camera")),

    # --- 4. Marine electronics and navigation -------------------------------
    "marine_navigation": ("Marine navigation", (
        "marine gps", "handheld marine gps", "chartplotter", "marine navigation system",
        "nautical navigation equipment")),
    "marine_sonar": ("Marine sonar", (
        "fish finder", "marine sonar", "depth sounder", "underwater sonar", "portable sonar")),
    "dive_computers": ("Dive computers", (
        "dive computer", "freediving watch", "dive watch", "underwater compass", "decompression computer",
        "dive data transmitter")),
    "waterproof_wearables": ("Waterproof wearables", (
        "waterproof smartwatch", "swimming watch", "surf watch", "gps sports watch", "dive watch",
        "swim tracker")),
    "marine_communication": ("Marine communication", (
        "marine radio", "waterproof handheld radio", "vhf marine radio", "emergency marine communicator",
        "satellite messenger")),
    "waterproof_audio": ("Waterproof audio", (
        "waterproof headphones", "swimming headphones", "waterproof earbuds", "bone-conduction swimming headphones",
        "waterproof bluetooth speaker")),
    "marine_power": ("Marine power", (
        "marine battery", "portable power station", "waterproof power bank", "solar charger",
        "marine battery charger")),
    "marine_lighting": ("Marine lighting", (
        "dive light", "underwater flashlight", "waterproof headlamp", "underwater work light",
        "marine navigation light")),

    # --- 5. Beach essentials, storage and outdoor living --------------------
    "beach_bags": ("Beach bags", (
        "beach bag", "waterproof beach bag", "dry bag", "roll-top dry bag", "waterproof backpack",
        "mesh beach bag", "dive gear bag")),
    "beach_towels": ("Beach towels", (
        "beach towel", "quick-dry towel", "microfiber beach towel", "surf changing towel",
        "sand-resistant towel")),
    "beach_shade": ("Beach shade", (
        "beach umbrella", "sunshade", "beach canopy", "shade tent", "beach shelter", "portable sun shelter",
        "pop-up beach tent")),
    "beach_seating": ("Beach seating", (
        "beach chair", "folding beach chair", "low beach chair", "portable beach seat", "beach lounger")),
    "beach_cooling": ("Cooling and heat-management products", (
        "beach cooler", "insulated cooler", "soft cooler", "portable ice chest", "insulated beach bag",
        "cooling towel", "portable outdoor fan")),
    "beach_hydration": ("Beach hydration", (
        "water bottle", "insulated water bottle", "hydration flask", "tumbler", "insulated tumbler",
        "hydration backpack", "hydration pack", "water bladder")),
    "waterproof_storage": ("Waterproof storage", (
        "waterproof phone pouch", "waterproof phone case", "waterproof gear case", "floating waterproof case",
        "dry box", "waterproof equipment bag")),
    "beach_camping": ("Beach camping and outdoor recreation", (
        "beach tent", "camping hammock", "portable shower", "outdoor mat", "waterproof blanket",
        "beach camping shelter", "portable changing tent")),
    "beach_furniture": ("Beach furniture", (
        "portable beach table", "folding outdoor table", "beach storage cart", "beach wagon", "beach trolley",
        "portable outdoor furniture")),
    "sand_management": ("Sand management", (
        "sand-removal bag", "beach foot wash", "portable foot shower", "beach mat", "sand-free beach mat",
        "beach cleaning brush")),

    # --- 6. Sun protection, skincare and personal care ----------------------
    "sun_protection": ("Sun protection and outdoor skincare", (
        "sunscreen", "reef-safe sunscreen", "mineral sunscreen", "zinc oxide sunscreen", "spf lotion",
        "sunscreen stick", "water-resistant sunscreen", "sunblock")),
    "after_sun_care": ("After-sun care", (
        "after-sun lotion", "aloe vera gel", "sunburn relief", "after-sun moisturizer", "cooling after-sun gel",
        "sunburn care")),
    "water_sports_skincare": ("Water-sports skincare", (
        "saltwater skincare", "outdoor skincare", "water-resistant skincare", "surf skincare",
        "ocean sports skincare")),
    "beach_haircare": ("Haircare for sun and saltwater exposure", (
        "saltwater hair treatment", "after-swim shampoo", "chlorine-removal shampoo", "uv hair protection",
        "leave-in conditioner", "surf haircare")),
    "lip_protection": ("Lip protection", (
        "spf lip balm", "water-resistant lip balm", "sun protection lip balm", "zinc oxide lip balm")),
    "water_sports_eye_protection": ("Water-sports eye protection", (
        "polarized sunglasses", "floating sunglasses", "surf sunglasses", "water-sports sunglasses",
        "uv-protection eyewear", "sports sunglasses")),
    "insect_protection": ("Insect protection", (
        "insect repellent", "water-resistant insect repellent", "outdoor insect protection",
        "mosquito repellent")),
    "skin_protection": ("Skin protection", (
        "anti-chafing balm", "wetsuit lubricant", "blister prevention balm", "water-sports skin protection")),

    # --- 7. Fishing, boating, sailing and marine recreation -----------------
    "surf_fishing": ("Surf fishing", (
        "surf fishing rod", "surfcasting rod", "spinning reel", "fishing tackle", "saltwater fishing gear",
        "surf fishing reel")),
    "spearfishing": ("Spearfishing", (
        "speargun", "polespear", "spearfishing wetsuit", "dive float", "float line", "spearfishing mask",
        "underwater hunting equipment")),
    "saltwater_fishing": ("Saltwater fishing", (
        "saltwater fishing reel", "fishing rod", "marine fishing electronics", "fishing pliers",
        "waterproof tackle box", "fishing waders")),
    "boating": ("Boating", (
        "recreational boat", "inflatable boat", "rigid inflatable boat", "boat engine", "boat accessories",
        "marine equipment")),
    "sailing": ("Sailing", (
        "sailboat equipment", "sailing harness", "sailing gloves", "sailing wetsuit", "sailing life jacket",
        "sailing clothing")),
    "marine_paddling": ("Marine paddling", (
        "sea kayak", "touring kayak", "kayak paddle", "canoe", "paddling jacket", "kayak dry bag")),
    "boating_safety": ("Boating safety", (
        "life jacket", "personal flotation device", "marine emergency beacon", "rescue throw bag",
        "marine safety whistle")),
    "watercraft": ("Watercraft", (
        "personal watercraft", "jet ski", "electric surfboard", "motorized surfboard", "hydrofoil board",
        "electric hydrofoil")),
    "marine_maintenance": ("Marine maintenance", (
        "boat cleaning products", "marine-grade equipment", "wetsuit cleaner", "surfboard repair kit",
        "marine protective coating")),
    "underwater_exploration": ("Underwater exploration", (
        "underwater scooter", "diver propulsion vehicle", "underwater metal detector", "underwater locator",
        "marine exploration equipment")),

    # --- 8. Hydration, nutrition and outdoor performance --------------------
    # Generic protein powder / supplement brands are deliberately NOT here: a
    # general protein brand must not qualify just because athletes might take
    # it to the beach.
    "sports_hydration": ("Sports hydration", (
        "electrolyte powder", "electrolyte tablets", "sports drink", "hydration drink", "hydration mix",
        "hydration salts")),
    "endurance_nutrition": ("Endurance nutrition", (
        "energy gel", "energy bar", "carbohydrate drink mix", "endurance nutrition", "sports nutrition",
        "energy chews")),
    "outdoor_food_storage": ("Outdoor food storage", (
        "insulated cooler", "portable food container", "outdoor cooler", "insulated lunch bag",
        "camping food storage")),
    "outdoor_performance": ("Outdoor performance", (
        "cooling towel", "sun-protection clothing", "lightweight outdoor apparel", "portable exercise equipment",
        "outdoor training gear")),
    "recovery_products": ("Recovery products", (
        "muscle recovery balm", "massage gun", "foam roller", "recovery roller", "cooling recovery products")),
    "portable_fitness": ("Portable fitness", (
        "resistance bands", "suspension trainer", "portable pull-up bar", "agility ladder",
        "beach workout equipment")),
    "outdoor_supplements": ("Outdoor supplements", (
        "electrolyte supplements", "endurance supplements", "sports nutrition supplements",
        "hydration supplements")),

    # --- 9. Ocean conservation and sustainable beach products ---------------
    "ocean_conservation": ("Ocean-conscious consumer products", (
        "ocean cleanup equipment", "marine debris collection", "beach cleanup equipment",
        "ocean plastic recycling")),
    "sustainable_swimwear": ("Sustainable swimwear", (
        "recycled swimwear", "recycled nylon swimwear", "sustainable surfwear", "recycled polyester boardshorts")),
    "reusable_beach_products": ("Reusable beach products", (
        "reusable water bottle", "reusable beach cup", "reusable food container", "reusable beach accessories")),
    "marine_conservation_gear": ("Marine conservation gear", (
        "coral restoration equipment", "reef monitoring equipment", "marine research equipment",
        "underwater survey equipment")),
    "beach_cleanup": ("Beach cleanup", (
        "beach litter picker", "beach cleaning machine", "sand sifting equipment",
        "beach waste collection equipment")),
    "reef_conscious_personal_care": ("Reef-conscious personal care", (
        "reef-conscious sunscreen", "ocean-conscious skincare", "biodegradable outdoor soap",
        "reef-conscious personal care products")),

    # --- 10. Additional categories ------------------------------------------
    "water_sports_safety": ("Water-sports safety", (
        "impact vest", "water-sports helmet", "surf helmet", "rescue buoy", "flotation device",
        "marine first-aid kit")),
    "water_sports_training": ("Water-sports training", (
        "swim training equipment", "swim paddles", "resistance swim trainer", "swim parachute", "swim tether",
        "swim training snorkel")),
    "water_sports_transport": ("Water-sports transport", (
        "surfboard rack", "surfboard roof rack", "kayak roof rack", "paddleboard carrier", "wetsuit changing mat")),
    "beach_pets": ("Beach pet products", (
        "dog life jacket", "dog flotation vest", "dog cooling vest", "dog beach towel",
        "dog swimming accessories")),
    "marine_optics": ("Marine optics", (
        "waterproof binoculars", "marine binoculars", "diving mask with optical lenses",
        "underwater viewing equipment")),
    "portable_water_safety": ("Portable water safety", (
        "personal locator beacon", "satellite messenger", "emergency marine radio",
        "waterproof emergency communication device")),
    "coastal_travel_gear": ("Coastal travel gear", (
        "travel snorkel set", "compact dive gear", "travel wetsuit bag", "portable beach shelter",
        "waterproof travel backpack")),
}

# The first group to run: specialist brands in the core ocean sports.
PRIORITY_CATEGORIES: tuple[str, ...] = (
    "surfing_equipment", "freediving_equipment", "spearfishing_equipment",
    "scuba_diving_equipment", "snorkeling_equipment", "paddle_sports_equipment",
)

# Core product types whose Wikidata subclasses are also searched (Strategy A).
EXPAND_TERMS = frozenset([
    "surfboard", "wetsuit", "kayak", "swimsuit", "sunscreen", "action camera", "camera drone", "life jacket",
    "dive computer", "snorkel", "sunglasses",
])

_JOIN = r"[\s-]*"


def term_regex(term: str) -> str:
    """
    Flexible regex source for a product term: words may be joined by space,
    hyphen or nothing (rash guard = rashguard = rash-guard), the last word
    may be plural (flip-flop = flip flops).
    """
    tokens = [t for t in re.split(r"[\s\-]+", term.strip().lower()) if t]
    if not tokens:
        return ""
    body = _JOIN.join(re.escape(t) for t in tokens)
    return body if tokens[-1].endswith("s") else body + r"(?:e?s)?"


def normalize_term(term: str) -> str:
    """Canonical form for de-duplicating searches ("Rash-Guard" == "rash guard")."""
    return re.sub(r"[\s\-]+", " ", term.strip().lower())
