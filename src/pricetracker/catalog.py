"""Seed catalog of generic ("canonical") food products, in Spanish and Catalan.

Each offer read from a flyer or online shop is linked to one of these products, so that
"Plàtan de Canàries IGP" (Catalan flyer) and "Plátano de Canarias" (Spanish shop) compare
against each other. Synonyms are matched head-noun first (see ``matching.py``), so put the
noun first: "pechuga de pollo", not "pollo pechuga".

Base unit is what prices are normalized to: kg, L or ud (unit).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CatalogProduct:
    slug: str
    name_es: str
    name_ca: str
    category: str
    base_unit: str
    synonyms: tuple[str, ...] = field(default=())


FRUTA = "Fruta"
VERDURA = "Verdura y hortalizas"
CARNE = "Carne"
PESCADO = "Pescado y marisco"
HUEVOS = "Huevos"
LACTEOS = "Lácteos"
CHARCUTERIA = "Charcutería"
PANADERIA = "Panadería y bollería"
DESPENSA = "Despensa"
CONSERVAS = "Conservas"
ACEITE = "Aceite y condimentos"
DESAYUNO = "Desayuno y dulces"
SNACKS = "Frutos secos y snacks"
BEBIDAS = "Bebidas"
CONGELADOS = "Congelados"


def _p(slug, es, ca, cat, unit, *syn):
    return CatalogProduct(slug, es, ca, cat, unit, tuple(syn))


CATALOG: tuple[CatalogProduct, ...] = (
    # --- fruta -----------------------------------------------------------------------------------
    _p("platano", "Plátano", "Plàtan", FRUTA, "kg", "platano de canarias", "platan de canaries"),
    _p("banana", "Banana", "Banana", FRUTA, "kg"),
    _p("manzana", "Manzana", "Poma", FRUTA, "kg"),
    _p("pera", "Pera", "Pera", FRUTA, "kg"),
    _p("naranja", "Naranja", "Taronja", FRUTA, "kg"),
    _p("mandarina", "Mandarina", "Mandarina", FRUTA, "kg", "clementina", "clemenvilla"),
    _p("limon", "Limón", "Llimona", FRUTA, "kg"),
    _p("uva", "Uva", "Raïm", FRUTA, "kg"),
    _p("fresa", "Fresa", "Maduixa", FRUTA, "kg", "freson", "maduixot"),
    _p("melon", "Melón", "Meló", FRUTA, "kg"),
    _p("sandia", "Sandía", "Síndria", FRUTA, "kg"),
    _p("melocoton", "Melocotón", "Préssec", FRUTA, "kg", "paraguayo", "paraguaia"),
    _p("nectarina", "Nectarina", "Nectarina", FRUTA, "kg"),
    _p("kiwi", "Kiwi", "Kiwi", FRUTA, "kg"),
    _p("pina", "Piña", "Pinya", FRUTA, "kg"),
    _p("aguacate", "Aguacate", "Alvocat", FRUTA, "kg"),
    _p("mango", "Mango", "Mango", FRUTA, "kg"),
    _p("ciruela", "Ciruela", "Pruna", FRUTA, "kg"),
    _p("cereza", "Cereza", "Cirera", FRUTA, "kg"),
    _p("albaricoque", "Albaricoque", "Albercoc", FRUTA, "kg"),
    _p("arandano", "Arándanos", "Nabius", FRUTA, "kg", "arandano", "nabiu"),
    _p("frambuesa", "Frambuesa", "Gerd", FRUTA, "kg"),
    _p("granada", "Granada", "Magrana", FRUTA, "kg"),
    _p("caqui", "Caqui", "Caqui", FRUTA, "kg", "kaki", "persimon"),
    _p("higo", "Higo", "Figa", FRUTA, "kg"),
    # --- verdura ---------------------------------------------------------------------------------
    _p("tomate", "Tomate", "Tomàquet", VERDURA, "kg", "tomate pera", "tomate rama", "tomaquet de penjar"),
    _p("tomate-cherry", "Tomate cherry", "Tomàquet cherry", VERDURA, "kg", "tomate cherri", "cherry"),
    _p("patata", "Patata", "Patata", VERDURA, "kg"),
    _p("cebolla", "Cebolla", "Ceba", VERDURA, "kg", "cebolleta", "calcot"),
    _p("ajo", "Ajo", "All", VERDURA, "kg"),
    _p("zanahoria", "Zanahoria", "Pastanaga", VERDURA, "kg"),
    _p("pimiento", "Pimiento", "Pebrot", VERDURA, "kg"),
    _p("pimiento-rojo", "Pimiento rojo", "Pebrot vermell", VERDURA, "kg"),
    _p("pimiento-verde", "Pimiento verde", "Pebrot verd", VERDURA, "kg", "pimiento italiano", "pebrot italia"),
    _p("calabacin", "Calabacín", "Carbassó", VERDURA, "kg"),
    _p("berenjena", "Berenjena", "Albergínia", VERDURA, "kg"),
    _p("pepino", "Pepino", "Cogombre", VERDURA, "kg"),
    _p("lechuga", "Lechuga", "Enciam", VERDURA, "ud", "lechuga iceberg", "lechuga romana"),
    _p("cogollos", "Cogollos", "Cabdells", VERDURA, "ud", "cogollo"),
    _p("brocoli", "Brócoli", "Bròquil", VERDURA, "kg"),
    _p("coliflor", "Coliflor", "Coliflor", VERDURA, "kg"),
    _p("espinacas", "Espinacas", "Espinacs", VERDURA, "kg"),
    _p("judia-verde", "Judía verde", "Mongeta tendra", VERDURA, "kg", "judias verdes", "mongetes tendres"),
    _p("champinon", "Champiñón", "Xampinyó", VERDURA, "kg", "champinones", "setas", "bolets"),
    _p("calabaza", "Calabaza", "Carbassa", VERDURA, "kg"),
    _p("puerro", "Puerro", "Porro", VERDURA, "kg"),
    _p("apio", "Apio", "Api", VERDURA, "kg"),
    _p("alcachofa", "Alcachofa", "Carxofa", VERDURA, "kg"),
    _p("boniato", "Boniato", "Moniato", VERDURA, "kg"),
    _p("ensalada", "Ensalada (bolsa)", "Amanida (bossa)", VERDURA, "kg", "mezcla de lechugas", "canonigos", "rucula"),
    _p("esparrago-verde", "Espárrago verde", "Espàrrec verd", VERDURA, "kg", "esparrago triguero"),
    # --- carne -----------------------------------------------------------------------------------
    _p("pechuga-pollo", "Pechuga de pollo", "Pit de pollastre", CARNE, "kg", "filete de pechuga de pollo", "filets de pit de pollastre"),
    _p("muslo-pollo", "Muslos de pollo", "Cuixes de pollastre", CARNE, "kg", "contramuslo de pollo", "jamoncitos de pollo"),
    _p("alitas-pollo", "Alitas de pollo", "Ales de pollastre", CARNE, "kg"),
    _p("pollo-entero", "Pollo entero", "Pollastre sencer", CARNE, "kg", "pollo", "pollastre"),
    _p("pavo", "Pavo (carne fresca)", "Gall dindi", CARNE, "kg", "filete de pavo", "pechuga de pavo fresca", "gall dindi"),
    _p("carne-picada", "Carne picada", "Carn picada", CARNE, "kg", "picada mixta", "preparado de carne picada"),
    _p("carne-picada-vacuno", "Carne picada de vacuno", "Carn picada de vedella", CARNE, "kg", "carne picada de ternera", "picada de vacuno"),
    _p("lomo-cerdo", "Lomo de cerdo", "Llom de porc", CARNE, "kg", "cinta de lomo", "lomo de cerdo fileteado"),
    _p("costillas-cerdo", "Costillas de cerdo", "Costella de porc", CARNE, "kg", "costilla de cerdo"),
    _p("solomillo-cerdo", "Solomillo de cerdo", "Filet de porc", CARNE, "kg"),
    _p("filete-ternera", "Filete de ternera", "Filet de vedella", CARNE, "kg", "bistec de ternera", "ternera", "vedella"),
    _p("hamburguesa", "Hamburguesas", "Hamburgueses", CARNE, "kg", "burger meat"),
    _p("butifarra", "Butifarra", "Botifarra", CARNE, "kg"),
    _p("salchichas-frescas", "Salchichas frescas", "Salsitxes fresques", CARNE, "kg", "longaniza fresca"),
    _p("cordero", "Cordero", "Xai", CARNE, "kg", "chuletas de cordero", "costelles de xai"),
    _p("conejo", "Conejo", "Conill", CARNE, "kg"),
    # --- pescado ---------------------------------------------------------------------------------
    _p("salmon", "Salmón", "Salmó", PESCADO, "kg", "lomos de salmon", "filete de salmon"),
    _p("merluza", "Merluza", "Lluç", PESCADO, "kg"),
    _p("bacalao", "Bacalao", "Bacallà", PESCADO, "kg"),
    _p("dorada", "Dorada", "Orada", PESCADO, "kg"),
    _p("lubina", "Lubina", "Llobarro", PESCADO, "kg"),
    _p("sardina", "Sardina", "Sardina", PESCADO, "kg", "sardina fresca"),
    _p("boqueron", "Boquerón", "Seitó", PESCADO, "kg"),
    _p("gamba", "Gambas", "Gambes", PESCADO, "kg", "gamba"),
    _p("langostino", "Langostinos", "Llagostins", PESCADO, "kg", "langostino"),
    _p("mejillon", "Mejillones", "Musclos", PESCADO, "kg", "mejillon"),
    _p("calamar", "Calamar", "Calamar", PESCADO, "kg", "anillas de calamar"),
    _p("sepia", "Sepia", "Sípia", PESCADO, "kg"),
    _p("atun-fresco", "Atún fresco", "Tonyina fresca", PESCADO, "kg", "lomo de atun", "atun rojo"),
    _p("pulpo", "Pulpo", "Pop", PESCADO, "kg"),
    # --- huevos ----------------------------------------------------------------------------------
    _p("huevos", "Huevos", "Ous", HUEVOS, "ud", "huevo", "huevos frescos", "ou"),
    # --- lácteos ---------------------------------------------------------------------------------
    _p("leche-entera", "Leche entera", "Llet sencera", LACTEOS, "L"),
    _p("leche-semidesnatada", "Leche semidesnatada", "Llet semidesnatada", LACTEOS, "L"),
    _p("leche-desnatada", "Leche desnatada", "Llet desnatada", LACTEOS, "L"),
    _p("leche-sin-lactosa", "Leche sin lactosa", "Llet sense lactosa", LACTEOS, "L"),
    _p("bebida-avena", "Bebida de avena", "Beguda de civada", LACTEOS, "L"),
    _p("bebida-soja", "Bebida de soja", "Beguda de soja", LACTEOS, "L"),
    _p("yogur-natural", "Yogur natural", "Iogurt natural", LACTEOS, "kg"),
    _p("yogur-griego", "Yogur griego", "Iogurt grec", LACTEOS, "kg"),
    _p("yogur-sabores", "Yogur de sabores", "Iogurt de sabors", LACTEOS, "kg", "yogur", "iogurt", "yogur de fresa", "yogur de limon"),
    _p("mantequilla", "Mantequilla", "Mantega", LACTEOS, "kg"),
    _p("nata", "Nata", "Nata", LACTEOS, "L", "nata para cocinar", "nata para montar", "nata per cuinar"),
    _p("queso-fresco", "Queso fresco", "Formatge fresc", LACTEOS, "kg", "queso de burgos", "queso batido"),
    _p("queso-curado", "Queso curado", "Formatge curat", LACTEOS, "kg", "queso viejo", "queso manchego curado"),
    _p("queso-semicurado", "Queso semicurado", "Formatge semicurat", LACTEOS, "kg", "queso tierno", "queso manchego"),
    _p("queso-rallado", "Queso rallado", "Formatge ratllat", LACTEOS, "kg"),
    _p("queso-lonchas", "Queso en lonchas", "Formatge a llesques", LACTEOS, "kg", "queso sandwich", "queso lonchas"),
    _p("mozzarella", "Mozzarella", "Mozzarella", LACTEOS, "kg", "queso mozzarella"),
    # --- charcutería -----------------------------------------------------------------------------
    _p("jamon-serrano", "Jamón serrano", "Pernil serrà", CHARCUTERIA, "kg", "jamon curado", "pernil curat"),
    _p("jamon-iberico", "Jamón ibérico", "Pernil ibèric", CHARCUTERIA, "kg", "paleta iberica", "espatlla iberica"),
    _p("jamon-cocido", "Jamón cocido", "Pernil dolç", CHARCUTERIA, "kg", "jamon york", "pernil cuit", "fiambre de jamon"),
    _p("pavo-fiambre", "Fiambre de pavo", "Embotit de gall dindi", CHARCUTERIA, "kg", "pechuga de pavo", "pavo en lonchas", "pit de gall dindi"),
    _p("chorizo", "Chorizo", "Xoriço", CHARCUTERIA, "kg"),
    _p("salchichon", "Salchichón", "Salsitxó", CHARCUTERIA, "kg"),
    _p("fuet", "Fuet", "Fuet", CHARCUTERIA, "kg", "espetec", "longaniza de vic", "llonganissa"),
    _p("bacon", "Bacon", "Bacó", CHARCUTERIA, "kg", "beicon", "panceta ahumada"),
    _p("salchichas", "Salchichas Frankfurt", "Salsitxes Frankfurt", CHARCUTERIA, "kg", "salchichas cocidas", "frankfurt", "frankfurts"),
    # --- panadería -------------------------------------------------------------------------------
    _p("pan-barra", "Barra de pan", "Barra de pa", PANADERIA, "kg", "baguette", "pan baguette"),
    _p("pan-molde", "Pan de molde", "Pa de motlle", PANADERIA, "kg"),
    _p("pan-molde-integral", "Pan de molde integral", "Pa de motlle integral", PANADERIA, "kg"),
    _p("pan-tostado", "Pan tostado", "Pa torrat", PANADERIA, "kg", "biscotes", "tostadas"),
    _p("croissant", "Croissant", "Croissant", PANADERIA, "kg", "cruasan"),
    _p("magdalenas", "Magdalenas", "Magdalenes", PANADERIA, "kg", "magdalena"),
    # --- despensa --------------------------------------------------------------------------------
    _p("arroz", "Arroz", "Arròs", DESPENSA, "kg", "arroz redondo", "arroz largo", "arroz bomba"),
    _p("pasta", "Pasta seca", "Pasta seca", DESPENSA, "kg", "pasta", "fideos", "fideus", "penne", "helices", "tallarines", "lazos"),
    _p("macarrones", "Macarrones", "Macarrons", DESPENSA, "kg"),
    _p("espaguetis", "Espaguetis", "Espaguetis", DESPENSA, "kg", "spaghetti", "espagueti"),
    _p("harina", "Harina de trigo", "Farina de blat", DESPENSA, "kg", "harina", "farina"),
    _p("azucar", "Azúcar", "Sucre", DESPENSA, "kg", "azucar blanco", "azucar blanquilla"),
    _p("sal", "Sal", "Sal", DESPENSA, "kg"),
    _p("lentejas", "Lentejas", "Llenties", DESPENSA, "kg"),
    _p("garbanzos", "Garbanzos", "Cigrons", DESPENSA, "kg"),
    _p("alubias", "Alubias", "Mongetes", DESPENSA, "kg", "judias blancas", "alubia", "fesols"),
    _p("copos-avena", "Copos de avena", "Flocs de civada", DESAYUNO, "kg", "avena"),
    # --- conservas -------------------------------------------------------------------------------
    _p("tomate-frito", "Tomate frito", "Tomàquet fregit", CONSERVAS, "kg"),
    _p("tomate-triturado", "Tomate triturado", "Tomàquet triturat", CONSERVAS, "kg", "tomate natural triturado"),
    _p("atun-lata", "Atún en conserva", "Tonyina en conserva", CONSERVAS, "kg", "atun", "atun claro", "atun en aceite", "tonyina", "bonito del norte"),
    _p("sardinas-lata", "Sardinas en conserva", "Sardines en conserva", CONSERVAS, "kg", "sardinas en aceite", "sardinillas"),
    _p("aceitunas", "Aceitunas", "Olives", CONSERVAS, "kg", "aceituna"),
    # --- aceite y condimentos --------------------------------------------------------------------
    _p("aceite-oliva-virgen-extra", "Aceite de oliva virgen extra", "Oli d'oliva verge extra", ACEITE, "L", "aove", "aceite de oliva virgen extra"),
    _p("aceite-oliva-virgen", "Aceite de oliva virgen", "Oli d'oliva verge", ACEITE, "L"),
    _p("aceite-oliva", "Aceite de oliva", "Oli d'oliva", ACEITE, "L", "aceite de oliva suave", "aceite de oliva intenso"),
    _p("aceite-girasol", "Aceite de girasol", "Oli de gira-sol", ACEITE, "L", "oli de girasol"),
    _p("vinagre", "Vinagre", "Vinagre", ACEITE, "L"),
    _p("mayonesa", "Mayonesa", "Maionesa", ACEITE, "kg"),
    _p("ketchup", "Ketchup", "Ketchup", ACEITE, "kg", "tomate ketchup"),
    _p("caldo", "Caldo", "Brou", ACEITE, "L"),
    # --- desayuno y dulces -----------------------------------------------------------------------
    _p("cafe-molido", "Café molido", "Cafè molt", DESAYUNO, "kg"),
    _p("cafe-grano", "Café en grano", "Cafè en gra", DESAYUNO, "kg"),
    _p("cafe-capsulas", "Café en cápsulas", "Cafè en càpsules", DESAYUNO, "ud", "capsulas de cafe", "capsules de cafe"),
    _p("cacao-soluble", "Cacao soluble", "Cacau soluble", DESAYUNO, "kg", "cacao en polvo"),
    _p("crema-cacao", "Crema de cacao", "Crema de cacau", DESAYUNO, "kg", "crema de cacao con avellanas"),
    _p("cereales", "Cereales", "Cereals", DESAYUNO, "kg", "corn flakes", "muesli", "granola"),
    _p("galletas", "Galletas", "Galetes", DESAYUNO, "kg", "galleta"),
    _p("chocolate", "Chocolate", "Xocolata", DESAYUNO, "kg", "tableta de chocolate"),
    _p("miel", "Miel", "Mel", DESAYUNO, "kg"),
    _p("mermelada", "Mermelada", "Melmelada", DESAYUNO, "kg", "confitura"),
    # --- frutos secos y snacks -------------------------------------------------------------------
    _p("nueces", "Nueces", "Nous", SNACKS, "kg", "nuez"),
    _p("almendras", "Almendras", "Ametlles", SNACKS, "kg", "almendra"),
    _p("cacahuetes", "Cacahuetes", "Cacauets", SNACKS, "kg", "cacahuete"),
    _p("pistachos", "Pistachos", "Festucs", SNACKS, "kg", "pistacho"),
    _p("patatas-chips", "Patatas fritas (chips)", "Patates xips", SNACKS, "kg", "patatas fritas", "patatas chips", "patates fregides", "chips"),
    # --- bebidas ---------------------------------------------------------------------------------
    _p("agua", "Agua mineral", "Aigua mineral", BEBIDAS, "L", "agua", "aigua"),
    _p("refresco-cola", "Refresco de cola", "Refresc de cola", BEBIDAS, "L", "cola", "coca cola", "pepsi"),
    _p("refresco", "Refresco", "Refresc", BEBIDAS, "L", "gaseosa", "tonica", "refresco de naranja", "refresco de limon"),
    _p("zumo-naranja", "Zumo de naranja", "Suc de taronja", BEBIDAS, "L"),
    _p("zumo", "Zumo", "Suc", BEBIDAS, "L", "nectar"),
    _p("cerveza", "Cerveza", "Cervesa", BEBIDAS, "L"),
    _p("cerveza-sin", "Cerveza sin alcohol", "Cervesa sense alcohol", BEBIDAS, "L", "cerveza 0 0", "cervesa 0 0"),
    _p("vino-tinto", "Vino tinto", "Vi negre", BEBIDAS, "L"),
    _p("vino-blanco", "Vino blanco", "Vi blanc", BEBIDAS, "L"),
    _p("cava", "Cava", "Cava", BEBIDAS, "L"),
    # --- congelados ------------------------------------------------------------------------------
    _p("guisantes", "Guisantes", "Pèsols", CONGELADOS, "kg", "guisante"),
    _p("patatas-prefritas", "Patatas prefritas congeladas", "Patates prefregides congelades", CONGELADOS, "kg", "patatas prefritas", "patatas fritas congeladas"),
    _p("pizza", "Pizza", "Pizza", CONGELADOS, "kg"),
    _p("croquetas", "Croquetas", "Croquetes", CONGELADOS, "kg", "croqueta"),
    _p("helado", "Helado", "Gelat", CONGELADOS, "L"),
)

CATEGORIES: tuple[str, ...] = tuple(dict.fromkeys(p.category for p in CATALOG))


@dataclass(frozen=True)
class SeedStore:
    slug: str
    name: str
    website: str


STORES: tuple[SeedStore, ...] = (
    SeedStore("mercadona", "Mercadona", "https://tienda.mercadona.es"),
    SeedStore("lidl", "Lidl", "https://www.lidl.es"),
    SeedStore("aldi", "Aldi", "https://www.aldi.es"),
    SeedStore("carrefour", "Carrefour", "https://www.carrefour.es"),
    SeedStore("dia", "Dia", "https://www.dia.es"),
    SeedStore("alcampo", "Alcampo", "https://www.compraonline.alcampo.es"),
    SeedStore("eroski", "Eroski", "https://supermercado.eroski.es"),
    SeedStore("consum", "Consum", "https://tienda.consum.es"),
    SeedStore("bonpreu", "Bonpreu / Esclat", "https://www.compraonline.bonpreuesclat.cat"),
    SeedStore("condis", "Condis", "https://www.condisline.com"),
    SeedStore("caprabo", "Caprabo", "https://www.capraboacasa.com"),
    SeedStore("bonarea", "bonÀrea", "https://www.bonarea-online.com"),
    SeedStore("ahorramas", "Ahorramás", "https://www.ahorramas.com"),
    SeedStore("hipercor", "Hipercor / El Corte Inglés", "https://www.elcorteingles.es/supermercado"),
    SeedStore("plusfresc", "Plusfresc", "https://www.plusfresc.cat"),
    SeedStore("spar", "Spar", "https://www.spar.es"),
)
