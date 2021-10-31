import uuid

import numpy as np
import wurst
from wurst import transformations as wt

from . import DATA_DIR
from .activity_maps import InventorySet
from .geomap import Geomap
from .utils import *

CROP_CLIMATE_MAP = DATA_DIR / "fuels" / "crop_climate_mapping.csv"
REGION_CLIMATE_MAP = DATA_DIR / "fuels" / "region_climate_mapping.csv"
FUEL_LABELS = DATA_DIR / "fuels" / "fuel_labels.csv"


class Fuels:
    """
    Class that modifies fuel inventories and markets in ecoinvent based on IAM output data.

    :ivar scenario: name of an IAM pathway
    :vartype pathway: str

    """

    def __init__(self, db, original_db, iam_data, model, pathway, year, regions=None):
        self.db = db
        self.original_db = original_db
        self.iam_data = iam_data
        self.model = model
        self.geo = Geomap(model=model)
        self.scenario = pathway
        self.year = year
        self.fuel_labels = self.iam_data.fuel_markets.coords["variables"].values
        self.list_iam_regions = (
            regions or iam_data.data.coords["region"].values.tolist()
        )
        mapping = InventorySet(self.db)
        self.fuels_map = mapping.generate_fuel_map()
        self.fuel_properties = get_fuel_properties()
        self.crops_properties = get_crops_properties()
        self.new_fuel_markets = {}

    def get_crop_climate_mapping(self):
        """Returns a dictionnary that indicates the type of crop
        used for bioethanol production per type of climate"""

        d = {}
        with open(CROP_CLIMATE_MAP) as f:
            r = csv.reader(f, delimiter=";")
            next(r)
            for line in r:
                climate, sugar, oil, wood, grass, grain = line
                d[climate] = {
                    "sugar": sugar,
                    "oil": oil,
                    "wood": wood,
                    "grass": grass,
                    "grain": grain,
                }
        return d

    def get_region_climate_mapping(self):
        """Returns a dicitonnary that indicates the type of climate
        for each IAM region"""

        d = {}
        with open(REGION_CLIMATE_MAP) as f:
            r = csv.reader(f, delimiter=";")
            next(r)
            for line in r:
                region, climate = line
                d[region] = climate
        return d

    def get_compression_effort(self, p_in, p_out, flow_rate):
        """Calculate the required electricity consumption from the compressor given
        an inlet and outlet pressure and a flow rate for hydrogen."""
        # result is shaft power [kW] and compressor size [kW]
        # flow_rate = mass flow rate (kg/day)
        # p_in =  input pressure (bar)
        # p_out =  output pressure (bar)
        Z_factor = 1.03198  # the hydrogen compressibility factor
        N_stages = 2  # the number of compressor stages (assumed to be 2 for this work)
        t_inlet = 310.95  # K the inlet temperature of the compressor
        y_ratio = 1.4  # the ratio of specific heats
        M_h2 = 2.15  # g/mol the molecular mass of hydrogen
        eff_comp = 0.75  # %
        R_constant = 8.314  # J/(mol*K)
        part_1 = (
            (flow_rate * (1 / (24 * 3600)))
            * ((Z_factor * t_inlet * R_constant) / (M_h2 * eff_comp))
            * ((N_stages * y_ratio / (y_ratio - 1)))
        )
        part_2 = ((p_out / p_in) ** ((y_ratio - 1) / (N_stages * y_ratio))) - 1
        power_req = part_1 * part_2
        motor_eff = 0.95
        oversizing = 1.1
        size_compressor = (power_req / motor_eff) * oversizing
        return power_req * 24 / flow_rate

    def generate_DAC_activities(self):

        """Generate regional variants of the DAC process with varying heat sources"""

        # define heat sources
        heat_map_ds = {
            "waste heat": (
                "heat, from municipal waste incineration to generic market for heat district or industrial, other than natural gas",
                "heat, district or industrial, other than natural gas",
            ),
            "industrial steam heat": (
                "market for heat, from steam, in chemical industry",
                "heat, from steam, in chemical industry",
            ),
            "heat pump heat": (
                "market group for electricity, low voltage",
                "electricity, low voltage",
            ),
        }

        # loop through IAM regions
        for region in self.list_iam_regions:
            for heat in heat_map_ds:

                ds = wt.copy_to_new_location(
                    ws.get_one(
                        self.original_db,
                        ws.contains("name", "carbon dioxide, captured from atmosphere"),
                    ),
                    region,
                )

                new_name = ds["name"] + ", with " + heat + ", and grid electricity"

                ds["name"] = new_name

                for exc in ws.production(ds):
                    exc["name"] = new_name
                    if "input" in exc:
                        exc.pop("input")

                for exc in ws.technosphere(ds):
                    if "heat" in exc["name"]:
                        exc["name"] = heat_map_ds[heat][0]
                        exc["product"] = heat_map_ds[heat][1]
                        exc["location"] = "RoW"

                        if heat == "heat pump heat":
                            exc["unit"] = "kilowatt hour"
                            exc["amount"] *= 1 / (
                                2.9 * 3.6
                            )  # COP of 2.9 and MJ --> kWh
                            exc["location"] = "RER"

                            ds[
                                "comment"
                            ] = "Dataset generated by `premise`, initially based on Terlouw et al. 2021. "
                            ds["comment"] += (
                                "A CoP of 2.9 is assumed for the heat pump. But the heat pump itself is not"
                                + " considered here. "
                            )

                ds["comment"] += (
                    "The CO2 is compressed from 1 bar to 25 bar, "
                    + " for which 0.78 kWh is considered. Furthermore, there's a 2.1% loss on site"
                    + " and only a 1 km long pipeline transport."
                )

                ds = relink_technosphere_exchanges(
                    ds, self.db, self.model, contained=False
                )

                self.db.append(ds)

    def find_transport_activity(self, items_to_look_for, items_to_exclude, loc):

        try:
            ds = ws.get_one(
                self.db,
                *[ws.contains("name", i) for i in items_to_look_for],
                ws.doesnt_contain_any("name", items_to_exclude),
                ws.equals("location", loc),
            )
        except ws.NoResults:
            ds = ws.get_one(
                self.db,
                *[ws.contains("name", i) for i in items_to_look_for],
                ws.doesnt_contain_any("name", items_to_exclude),
            )

        return (ds["name"], ds["reference product"], ds["unit"], ds["location"])

    def generate_hydrogen_activities(self):
        """

        Defines regional variants for hydrogen production, but also different supply
        chain designs:
        * by truck (100, 200, 500 and 1000 km), gaseous, liquid and LOHC
        * by reassigned CNG pipeline (100, 200, 500 and 1000 km), gaseous, with and without inhibitors
        * by dedicated H2 pipeline (100, 200, 500 and 1000 km), gaseous
        * by ship, liquid (1000, 2000, 5000 km)

        For truck and pipeline supply chains, we assume a transmission and a distribution part, for which
        we have specific pipeline designs. We also assume a means for regional storage in between (salt cavern).
        We apply distance-based losses along the way.

        Most of these supply chain design options are based on the work:
        * Wulf C, Reuß M, Grube T, Zapp P, Robinius M, Hake JF, et al.
          Life Cycle Assessment of hydrogen transport and distribution options.
          J Clean Prod 2018;199:431–43. https://doi.org/10.1016/j.jclepro.2018.07.180.
        * Hank C, Sternberg A, Köppel N, Holst M, Smolinka T, Schaadt A, et al.
          Energy efficiency and economic assessment of imported energy carriers based on renewable electricity.
          Sustain Energy Fuels 2020;4:2256–73. https://doi.org/10.1039/d0se00067a.
        * Petitpas G. Boil-off losses along the LH2 pathway. US Dep Energy Off Sci Tech Inf 2018.

        We also assume efficiency gains over time for the PEM electrolysis process: from 58 kWh/kg H2 in 2010,
        down to 44 kWh by 2050, according to a literature review conducted by the Paul Scherrer Institut.

        """
        print("Generate region-specific hydrogen production pathways.")
        fuel_activities = {
            "hydrogen": [
                (
                    "hydrogen production, gaseous, 25 bar, from electrolysis",
                    "from electrolysis",
                ),
                (
                    "hydrogen production, steam methane reforming, from biomethane, high and low temperature, with CCS (MDEA, 98% eff.), 26 bar",
                    "from SMR of biogas, with CCS",
                ),
                (
                    "hydrogen production, steam methane reforming, from biomethane, high and low temperature, 26 bar",
                    "from SMR of biogas",
                ),
                (
                    "hydrogen production, auto-thermal reforming, from biomethane, 25 bar",
                    "from ATR of biogas",
                ),
                (
                    "hydrogen production, auto-thermal reforming, from biomethane, with CCS (MDEA, 98% eff.), 25 bar",
                    "from ATR of biogas, with CCS",
                ),
                (
                    "hydrogen production, steam methane reforming of natural gas, 25 bar",
                    "from SMR of nat. gas",
                ),
                (
                    "hydrogen production, steam methane reforming of natural gas, with CCS (MDEA, 98% eff.), 25 bar",
                    "from SMR of nat. gas, with CCS",
                ),
                (
                    "hydrogen production, auto-thermal reforming of natural gas, 25 bar",
                    "from ATR of nat. gas",
                ),
                (
                    "hydrogen production, auto-thermal reforming of natural gas, with CCS (MDEA, 98% eff.), 25 bar",
                    "from ATR of nat. gas, with CCS",
                ),
                (
                    "hydrogen production, gaseous, 25 bar, from heatpipe reformer gasification of woody biomass with CCS, at gasification plant",
                    "from gasification of biomass by heatpipe reformer, with CCS",
                ),
                (
                    "hydrogen production, gaseous, 25 bar, from heatpipe reformer gasification of woody biomass, at gasification plant",
                    "from gasification of biomass by heatpipe reformer",
                ),
                (
                    "hydrogen production, gaseous, 25 bar, from gasification of woody biomass in entrained flow gasifier, with CCS, at gasification plant",
                    "from gasification of biomass, with CCS",
                ),
                (
                    "hydrogen production, gaseous, 25 bar, from gasification of woody biomass in entrained flow gasifier, at gasification plant",
                    "from gasification of biomass",
                ),
                (
                    "hydrogen production, gaseous, 30 bar, from hard coal gasification and reforming, at coal gasification plant",
                    "from coal gasification",
                ),
            ]
        }

        for region in self.list_iam_regions:

            for fuel in fuel_activities:
                for f in fuel_activities[fuel]:

                    ds = wt.copy_to_new_location(
                        ws.get_one(self.original_db, ws.contains("name", f[0])), region
                    )

                    for exc in ws.production(ds):
                        if "input" in exc:
                            exc.pop("input")

                    # we adjust the electrolysis efficiency
                    # from 58 kWh/kg H2 in 2010, down to 44 kWh in 2050
                    if (
                        f[0]
                        == "hydrogen production, gaseous, 25 bar, from electrolysis"
                    ):
                        for exc in ws.technosphere(ds):
                            if "market group for electricity" in exc["name"]:
                                exc["amount"] = -0.3538 * (self.year - 2010) + 58.589

                        string = f" The electricity input per kg of H2 has been adapted to the year {self.year}."
                        if "comment" in ds:
                            ds["comment"] += string
                        else:
                            ds["comment"] = string

                    ds = relink_technosphere_exchanges(ds, self.db, self.model)

                    ds[
                        "comment"
                    ] = "Region-specific hydrogen production dataset generated by `premise`. "

                    self.db.append(ds)

        print("Generate region-specific hydrogen supply chains.")
        # loss coefficients for hydrogen supply
        losses = {
            "truck": {
                "gaseous": (
                    lambda d: 0.005,  # compression, per operation,
                    f" 0.5% loss during compression.",
                ),
                "liquid": (
                    lambda d: (
                        0.013  # liquefaction, per operation
                        + 0.02  # vaporization, per operation
                        + np.power(1.002, d / 50 / 24)
                        - 1  # boil-off, per day, 50 km/h on average
                    ),
                    "1.3% loss during liquefaction. Boil-off loss of 0.2% per day of truck driving. "
                    "2% loss caused by vaporization during tank filling at fuelling station.",
                ),
                "liquid organic compound": (
                    lambda d: 0.005,
                    "0.5% loss during hydrogenation.",
                ),
            },
            "ship": {
                "liquid": (
                    lambda d: (
                        0.013  # liquefaction, per operation
                        + 0.02  # vaporization, per operation
                        + np.power(
                            0.2, d / 36 / 24
                        )  # boil-off, per day, 36 km/h on average
                    ),
                    "1.3% loss during liquefaction. Boil-off loss of 0.2% per day of shipping. "
                    "2% loss caused by vaporization during tank filling at fuelling station.",
                ),
            },
            "H2 pipeline": {
                "gaseous": (
                    lambda d: (
                        0.005  # compression, per operation
                        + 0.023  # storage, unused buffer gas
                        + 0.01  # storage, yearly leakage rate
                        + 4e-5 * d  # pipeline leakage, per km
                    ),
                    "0.5% loss during compression. 3.3% loss at regional storage."
                    "Leakage rate of 4e-5 kg H2 per km of pipeline.",
                )
            },
            "CNG pipeline": {
                "gaseous": (
                    lambda d: (
                        0.005  # compression, per operation
                        + 0.023  # storage, unused buffer gas
                        + 0.01  # storage, yearly leakage rate
                        + 4e-5 * d  # pipeline leakage, per km
                        + 0.07  # purification, per operation
                    ),
                    "0.5% loss during compression. 3.3% loss at regional storage."
                    "Leakage rate of 4e-5 kg H2 per km of pipeline. 7% loss during sepration of H2"
                    "from inhibitor gas.",
                )
            },
        }

        supply_chain_scenarios = {
            "truck": {
                "type": [
                    (
                        "market for transport, freight, lorry, unspecified",
                        "transport, freight, lorry, unspecified",
                        "ton kilometer",
                        "RER",
                    )
                ],
                "state": ["gaseous", "liquid", "liquid organic compound"],
                "distance": [500, 1000],
            },
            "ship": {
                "type": [
                    self.find_transport_activity(
                        items_to_look_for=[
                            "market for transport, freight, sea",
                            "liquefied",
                        ],
                        items_to_exclude=["other"],
                        loc="RoW",
                    )
                ],
                "state": ["liquid"],
                "distance": [2000, 5000],
            },
            "H2 pipeline": {
                "type": [
                    (
                        "distribution pipeline for hydrogen, dedicated hydrogen pipeline",
                        "pipeline, for hydrogen distribution",
                        "kilometer",
                        "RER",
                    ),
                    (
                        "transmission pipeline for hydrogen, dedicated hydrogen pipeline",
                        "pipeline, for hydrogen transmission",
                        "kilometer",
                        "RER",
                    ),
                ],
                "state": ["gaseous"],
                "distance": [500, 1000],
                "regional storage": (
                    "geological hydrogen storage",
                    "hydrogen storage",
                    "kilogram",
                    "RER",
                ),
                "lifetime": 40 * 400000 * 1e3,
            },
            "CNG pipeline": {
                "type": [
                    (
                        "distribution pipeline for hydrogen, reassigned CNG pipeline",
                        "pipeline, for hydrogen distribution",
                        "kilometer",
                        "RER",
                    ),
                    (
                        "transmission pipeline for hydrogen, reassigned CNG pipeline",
                        "pipeline, for hydrogen transmission",
                        "kilometer",
                        "RER",
                    ),
                ],
                "state": ["gaseous"],
                "distance": [500, 1000],
                "regional storage": (
                    "geological hydrogen storage",
                    "hydrogen storage",
                    "kilogram",
                    "RER",
                ),
                "lifetime": 40 * 400000 * 1e3,
            },
        }

        for region in self.list_iam_regions:

            for act in [
                "hydrogen embrittlement inhibition",
                "geological hydrogen storage",
                "hydrogenation of hydrogen",
                "dehydrogenation of hydrogen",
                "Hydrogen refuelling station",
            ]:

                ds = wt.copy_to_new_location(
                    ws.get_one(self.original_db, ws.equals("name", act)), region
                )

                for exc in ws.production(ds):
                    if "input" in exc:
                        exc.pop("input")

                ds = relink_technosphere_exchanges(ds, self.db, self.model)

                self.db.append(ds)

            for fuel in fuel_activities:
                for f in fuel_activities[fuel]:
                    for vehicle in supply_chain_scenarios:
                        for s in supply_chain_scenarios[vehicle]["state"]:
                            for d in supply_chain_scenarios[vehicle]["distance"]:

                                # dataset creation
                                new_act = {
                                    "location": region,
                                    "name": "hydrogen supply, "
                                    + f[1]
                                    + ", by "
                                    + vehicle
                                    + ", as "
                                    + s
                                    + ", over "
                                    + str(d)
                                    + " km",
                                    "reference product": "hydrogen, 700 bar",
                                    "unit": "kilogram",
                                    "database": self.db[1]["database"],
                                    "code": str(uuid.uuid4().hex),
                                    "comment": f"Dataset representing {fuel} supply, generated by `premise`.",
                                }

                                # production flow
                                new_exc = [
                                    {
                                        "uncertainty type": 0,
                                        "loc": 1,
                                        "amount": 1,
                                        "type": "production",
                                        "production volume": 1,
                                        "product": "hydrogen, 700 bar",
                                        "name": "hydrogen supply, "
                                        + f[1]
                                        + ", by "
                                        + vehicle
                                        + ", as "
                                        + s
                                        + ", over "
                                        + str(d)
                                        + " km",
                                        "unit": "kilogram",
                                        "location": region,
                                    }
                                ]

                                # transport
                                for t in supply_chain_scenarios[vehicle]["type"]:
                                    new_exc.append(
                                        {
                                            "uncertainty type": 0,
                                            "amount": d / 1000
                                            if t[2] == "ton kilometer"
                                            else d
                                            / 2
                                            * (
                                                1
                                                / supply_chain_scenarios[vehicle][
                                                    "lifetime"
                                                ]
                                            ),
                                            "type": "technosphere",
                                            "product": t[1],
                                            "name": t[0],
                                            "unit": t[2],
                                            "location": t[3],
                                            "comment": f"Transport over {d} km by {vehicle}.",
                                        }
                                    )

                                    string = f"Transport over {d} km by {vehicle}."

                                    if "comment" in new_act:
                                        new_act["comment"] += string
                                    else:
                                        new_act["comment"] = string

                                # need for inhibitor and purification if CNG pipeline
                                # electricity for purification: 2.46 kWh/kg H2
                                if vehicle == "CNG pipeline":

                                    inhibbitor_ds = ws.get_one(
                                        self.db,
                                        ws.contains(
                                            "name", "hydrogen embrittlement inhibition"
                                        ),
                                        ws.equals("location", region),
                                    )

                                    new_exc.append(
                                        {
                                            "uncertainty type": 0,
                                            "amount": 1,
                                            "type": "technosphere",
                                            "product": inhibbitor_ds[
                                                "reference product"
                                            ],
                                            "name": inhibbitor_ds["name"],
                                            "unit": inhibbitor_ds["unit"],
                                            "location": region,
                                            "comment": "Injection of an inhibiting gas (oxygen) to prevent embritllement of metal.",
                                        }
                                    )

                                    string = (
                                        " 2.46 kWh/kg H2 is needed to purify the hydrogen from the inhibiting gas."
                                        " The recovery rate for hydrogen after separation from the inhibitor gas is 93%."
                                    )
                                    if "comment" in new_act:
                                        new_act["comment"] += string
                                    else:
                                        new_act["comment"] = string

                                if (
                                    "regional storage"
                                    in supply_chain_scenarios[vehicle]
                                ):

                                    storage_ds = ws.get_one(
                                        self.db,
                                        ws.contains(
                                            "name", "geological hydrogen storage"
                                        ),
                                        ws.equals("location", region),
                                    )

                                    new_exc.append(
                                        {
                                            "uncertainty type": 0,
                                            "amount": 1,
                                            "type": "technosphere",
                                            "product": storage_ds["reference product"],
                                            "name": storage_ds["name"],
                                            "unit": storage_ds["unit"],
                                            "location": region,
                                            "comment": "Geological storage (salt cavern).",
                                        }
                                    )

                                    string = " Geological storage is added. It includes 0.344 kWh for the injection and pumping of 1 kg of H2."
                                    if "comment" in new_act:
                                        new_act["comment"] += string
                                    else:
                                        new_act["comment"] = string

                                # electricity for compression
                                if s == "gaseous":

                                    # if transport by truck, compression from 25 bar to 500 bar for teh transport
                                    # and from 500 bar to 900 bar for dispensing in 700 bar storage tanks

                                    # if transport by pipeline, initial compression from 25 bar to 100 bar
                                    # and 0.6 kWh re-compression every 250 km
                                    # and finally from 100 bar to 900 bar for dispensing in 700 bar storage tanks

                                    if vehicle == "truck":
                                        electricity_comp = self.get_compression_effort(
                                            25, 500, 1000
                                        )
                                        electricity_comp += self.get_compression_effort(
                                            500, 900, 1000
                                        )
                                    else:
                                        electricity_comp = self.get_compression_effort(
                                            25, 100, 1000
                                        ) + (0.6 * d / 250)
                                        electricity_comp += self.get_compression_effort(
                                            100, 900, 1000
                                        )

                                    new_exc.append(
                                        {
                                            "uncertainty type": 0,
                                            "amount": electricity_comp,
                                            "type": "technosphere",
                                            "product": "electricity, low voltage",
                                            "name": "market group for electricity, low voltage",
                                            "unit": "kilowatt hour",
                                            "location": "RoW",
                                        }
                                    )

                                    string = (
                                        f" {electricity_comp} kWh is added to compress from 25 bar 100 bar (if pipeline)"
                                        f"or 500 bar (if truck), and then to 900 bar to dispense in storage tanks at 700 bar. "
                                        " Additionally, if transported by pipeline, there is re-compression (0.6 kWh) every 250 km."
                                    )

                                    if "comment" in new_act:
                                        new_act["comment"] += string
                                    else:
                                        new_act["comment"] = string

                                # electricity for liquefaction
                                if s == "liquid":
                                    # liquefaction electricity need
                                    # currently, 12 kWh/kg H2
                                    # mid-term, 8 kWh/ kg H2
                                    # by 2050, 6 kWh/kg H2
                                    electricity_comp = np.clip(
                                        np.interp(
                                            self.year, [2020, 2035, 2050], [12, 8, 6]
                                        ),
                                        12,
                                        6,
                                    )
                                    new_exc.append(
                                        {
                                            "uncertainty type": 0,
                                            "amount": electricity_comp,
                                            "type": "technosphere",
                                            "product": "electricity, low voltage",
                                            "name": "market group for electricity, low voltage",
                                            "unit": "kilowatt hour",
                                            "location": "RoW",
                                        }
                                    )

                                    string = f" {electricity_comp} kWh is added to liquefy the hydrogen. "
                                    if "comment" in new_act:
                                        new_act["comment"] += string
                                    else:
                                        new_act["comment"] = string

                                # electricity for hydrogenation, dehydrogenation and compression at delivery
                                if s == "liquid organic compound":

                                    hydrogenation_ds = ws.get_one(
                                        self.db,
                                        ws.equals("name", "hydrogenation of hydrogen"),
                                        ws.equals("location", region),
                                    )

                                    dehydrogenation_ds = ws.get_one(
                                        self.db,
                                        ws.equals(
                                            "name", "dehydrogenation of hydrogen"
                                        ),
                                        ws.equals("location", region),
                                    )

                                    new_exc.extend(
                                        [
                                            {
                                                "uncertainty type": 0,
                                                "amount": 1,
                                                "type": "technosphere",
                                                "product": hydrogenation_ds[
                                                    "reference product"
                                                ],
                                                "name": hydrogenation_ds["name"],
                                                "unit": hydrogenation_ds["unit"],
                                                "location": region,
                                            },
                                            {
                                                "uncertainty type": 0,
                                                "amount": 1,
                                                "type": "technosphere",
                                                "product": dehydrogenation_ds[
                                                    "reference product"
                                                ],
                                                "name": dehydrogenation_ds["name"],
                                                "unit": dehydrogenation_ds["unit"],
                                                "location": region,
                                            },
                                        ]
                                    )

                                    # After dehydrogenation at ambient temperature at delivery
                                    # the hydrogen needs to be compressed up to 900 bar to be dispensed
                                    # in 700 bar storage tanks

                                    electricity_comp = self.get_compression_effort(
                                        25, 900, 1000
                                    )

                                    new_exc.append(
                                        {
                                            "uncertainty type": 0,
                                            "amount": electricity_comp,
                                            "type": "technosphere",
                                            "product": "electricity, low voltage",
                                            "name": "market group for electricity, low voltage",
                                            "unit": "kilowatt hour",
                                            "location": "RoW",
                                        }
                                    )

                                    string = (
                                        " Hydrogenation and dehydrogenation of hydrogen included. "
                                        "Compression at delivery after dehydrogenation also included."
                                    )
                                    if "comment" in new_act:
                                        new_act["comment"] += string
                                    else:
                                        new_act["comment"] = string

                                # fetch the H2 production activity
                                h2_ds = ws.get_one(
                                    self.db,
                                    ws.equals("name", f[0]),
                                    ws.equals("location", region),
                                )

                                # include losses along the way
                                new_exc.append(
                                    {
                                        "uncertainty type": 0,
                                        "amount": 1 + losses[vehicle][s][0](d),
                                        "type": "technosphere",
                                        "product": h2_ds["reference product"],
                                        "name": h2_ds["name"],
                                        "unit": h2_ds["unit"],
                                        "location": region,
                                    }
                                )

                                string = losses[vehicle][s][1]
                                if "comment" in new_act:
                                    new_act["comment"] += string
                                else:
                                    new_act["comment"] = string

                                # add fuelling station, including storage tank
                                ds_h2_station = ws.get_one(
                                    self.db,
                                    ws.equals("name", "Hydrogen refuelling station"),
                                    ws.equals("location", region),
                                )

                                new_exc.append(
                                    {
                                        "uncertainty type": 0,
                                        "amount": 1
                                        / (
                                            600 * 365 * 40
                                        ),  # 1 over lifetime: 40 years, 600 kg H2/day
                                        "type": "technosphere",
                                        "product": ds_h2_station["reference product"],
                                        "name": ds_h2_station["name"],
                                        "unit": ds_h2_station["unit"],
                                        "location": region,
                                    }
                                )

                                # finally, add pre-cooling
                                # pre-cooling is needed before filling vehicle tanks
                                # as the hydrogen is pumped, the ambient temperature
                                # vaporizes the gas, and because of the Thomson-Joule effect,
                                # the gas temperature increases.
                                # Hence, a refrigerant is needed to keep the H2 as low as
                                # -30 C during pumping.

                                # https://www.osti.gov/servlets/purl/1422579 gives us a formula
                                # to estimate pre-cooling electricity need
                                # it requires a capacity utilization for the fuellnig station
                                # as well as an ambient temperature
                                # we will use a temp of 25 C
                                # and a capacity utilization going from 10 kg H2/day in 2020
                                # to 150 kg H2/day in 2050
                                t_amb = 25
                                cap_util = np.interp(self.year, [2020, 2050], [10, 150])
                                el_pre_cooling = (
                                    0.3 / 1.6 * np.exp(-0.018 * t_amb)
                                ) + ((25 * np.log(t_amb) - 21) / cap_util)

                                new_exc.append(
                                    {
                                        "uncertainty type": 0,
                                        "amount": el_pre_cooling,
                                        "type": "technosphere",
                                        "product": "electricity, low voltage",
                                        "name": "market group for electricity, low voltage",
                                        "unit": "kilowatt hour",
                                        "location": "RoW",
                                    }
                                )

                                string = (
                                    f"Pre-cooling electricity is considered ({el_pre_cooling}), "
                                    f"assuming an ambiant temperature of {t_amb}C "
                                    f"and a capacity utilization for the fuel station of {cap_util} kg/day."
                                )
                                if "comment" in new_act:
                                    new_act["comment"] += string
                                else:
                                    new_act["comment"] = string

                                new_act["exchanges"] = new_exc

                                new_act = relink_technosphere_exchanges(
                                    new_act, self.db, self.model
                                )

                                self.db.append(new_act)

    def generate_biogas_activities(self):

        fuel_activities = {
            "methane, from biomass": [
                "production of 2 wt-% potassium",
                "biogas upgrading - sewage sludge",
                "Biomethane, gaseous",
            ],
            "methane, synthetic": [
                "methane, from electrochemical methanation, with carbon from atmospheric CO2 capture",
                "Methane, synthetic, gaseous, 5 bar, from electrochemical methanation, at fuelling station",
            ],
        }

        for region in self.list_iam_regions:
            for fuel in fuel_activities:
                for f in fuel_activities[fuel]:
                    if fuel == "methane, synthetic":

                        for CO2_type in [
                            (
                                "carbon dioxide, captured from atmosphere, with waste heat, and grid electricity",
                                "carbon dioxide, captured from the atmosphere",
                                "waste heat",
                            ),
                            # ("carbon dioxide, captured from atmosphere, with industrial steam heat, and grid electricity", "carbon dioxide, captured from atmosphere", "industrial steam heat"),
                            (
                                "carbon dioxide, captured from atmosphere, with heat pump heat, and grid electricity",
                                "carbon dioxide, captured from the atmosphere",
                                "heat pump heat",
                            ),
                        ]:
                            ds = wt.copy_to_new_location(
                                ws.get_one(self.original_db, ws.contains("name", f)),
                                region,
                            )

                            for exc in ws.production(ds):
                                if "input" in exc:
                                    exc.pop("input")

                            for exc in ws.technosphere(ds):
                                if (
                                    "carbon dioxide, captured from atmosphere"
                                    in exc["name"]
                                ):
                                    exc["name"] = CO2_type[0]
                                    exc["product"] = CO2_type[1]
                                    exc["location"] = region

                                    ds["name"] += "using " + CO2_type[2]

                                    for prod in ws.production(ds):
                                        prod["name"] += "using " + CO2_type[2]

                                if (
                                    "methane, from electrochemical methanation"
                                    in exc["name"]
                                ):
                                    exc["name"] += "using " + CO2_type[2]

                                    ds["name"] = ds["name"].replace(
                                        "from electrochemical methanation",
                                        f"from electrochemical methanation (H2 from electrolysis, CO2 from DAC using {CO2_type[2]})",
                                    )

                                    for prod in ws.production(ds):
                                        prod["name"] = prod["name"].replace(
                                            "from electrochemical methanation",
                                            f"from electrochemical methanation (H2 from electrolysis, CO2 from DAC using {CO2_type[2]})",
                                        )

                            ds = relink_technosphere_exchanges(ds, self.db, self.model)

                            self.db.append(ds)

                    else:

                        ds = wt.copy_to_new_location(
                            ws.get_one(self.original_db, ws.contains("name", f)), region
                        )

                        for exc in ws.production(ds):
                            if "input" in exc:
                                exc.pop("input")

                        ds = relink_technosphere_exchanges(ds, self.db, self.model)

                        self.db.append(ds)

    def generate_synthetic_fuel_activities(self):

        fuel_activities = {
            "methanol": ["methanol", "hydrogen from electrolysis", "energy allocation"],
            "methanol, from coal": [
                "methanol",
                "hydrogen from coal gasification",
                "energy allocation",
            ],
            "fischer-tropsch": [
                "Fischer Tropsch process",
                "hydrogen from electrolysis",
                "energy allocation",
            ],
            "fischer-tropsch, from woody biomass": [
                "Fischer Tropsch process",
                "hydrogen from wood gasification",
                "energy allocation",
            ],
            "fischer-tropsch, from coal": [
                "Fischer Tropsch process",
                "hydrogen from coal gasification",
                "energy allocation",
            ],
        }

        for region in self.list_iam_regions:
            for fuel in fuel_activities:

                filter_ds = ws.get_many(
                    self.original_db,
                    *[ws.contains("name", n) for n in fuel_activities[fuel]],
                )

                for ds in filter_ds:

                    ds_copy = wt.copy_to_new_location(ds, region)

                    for exc in ws.production(ds_copy):
                        if "input" in exc:
                            exc.pop("input")

                    for exc in ws.technosphere(ds_copy):
                        if "carbon dioxide, captured from atmosphere" in exc["name"]:
                            exc[
                                "name"
                            ] = "carbon dioxide, captured from atmosphere, with heat pump heat, and grid electricity"
                            exc[
                                "product"
                            ] = "carbon dioxide, captured from the atmosphere"
                            exc["location"] = region

                    ds_copy = relink_technosphere_exchanges(
                        ds_copy, self.db, self.model
                    )

                    self.db.append(ds_copy)

    def get_biofuel_production_volume(self, region, fuel_label):
        """Fetch from the IAM data the consumption (or production) volume."""

        return (
            self.iam_data.data.sel(variables=fuel_label, region=region)
            .interp(year=self.year)
            .values
        )

    def generate_biofuel_activities(self):
        """
        Create region-specific biofuel datasets.
        Update the conversion efficiency.
        :return:
        """

        # region -> climate dictionary
        d_region_climate = self.get_region_climate_mapping()
        # climate --> {crop type --> crop} dictionary
        d_climate_crop_type = self.get_crop_climate_mapping()

        added_acts = []

        regions = (r for r in self.list_iam_regions if r != "World")
        for region in regions:
            climate_type = d_region_climate[region]

            for crop_type in d_climate_crop_type[climate_type]:
                crop = d_climate_crop_type[climate_type][crop_type]

                for original_ds in ws.get_many(
                    self.original_db,
                    ws.contains("name", crop),
                    ws.either(
                        *[
                            ws.contains("name", "supply of"),
                            ws.contains(
                                "name",
                                "via fermentation"
                                if crop_type != "oil"
                                else "via transesterification",
                            ),
                        ]
                    ),
                ):

                    ds = wt.copy_to_new_location(original_ds, region)

                    for exc in ws.production(ds):
                        if "input" in exc:
                            exc.pop("input")

                    ds = relink_technosphere_exchanges(ds, self.db, self.model)

                    # give it a production volume
                    for label in self.fuel_labels:
                        if crop_type in label:

                            ds["production volume"] = self.iam_data.fuel_markets.sel(
                                region=region, variables=label
                            ).values

                    # if this is a fuel conversion process
                    # we want to update the conversion efficiency
                    if any(
                        x in ds["name"] for x in ["fermentation", "transesterification"]
                    ) and any(
                        x in ds["name"]
                        for x in ["Ethanol production", "Biodiesel production"]
                    ):
                        # modify efficiency
                        # fetch the `progress factor`, compared to 2020

                        progress_factor = (
                            self.iam_data.fuel_efficiencies.sel(
                                region=region,
                                variables=[
                                    v
                                    for v in self.iam_data.fuel_efficiencies.coords[
                                        "variables"
                                    ].values
                                    if crop_type.lower() in v.lower()
                                    and any(
                                        x.lower() in v.lower()
                                        for x in ["bioethanol", "biodiesel"]
                                    )
                                ],
                            )
                            .sum(dim="variables")
                            .values
                        )

                        # Rescale all the technosphere exchanges according to the IAM efficiency values

                        wurst.change_exchanges_by_constant_factor(
                            ds, 1 / progress_factor
                        )

                        string = (
                            f"The process conversion efficiency has been rescaled by premise by {progress_factor}.\n"
                            f"To be in line with the pathway {self.scenario} of {self.model.upper()} "
                            f"in {self.year} in the region {region}.\n"
                        )

                        if "comment" in ds:
                            ds["comment"] += string
                        else:
                            ds["comment"] = string

                        if "ethanol" in ds["name"].lower():
                            ds[
                                "comment"
                            ] += "Bioethanol has a combustion CO2 emission factor of 1.91 kg CO2/kg."
                        if "biodiesel" in ds["name"].lower():
                            ds[
                                "comment"
                            ] += "Biodiesel has a combustion CO2 emission factor of 2.85 kg CO2/kg."

                    # if this is a farming activity
                    # and if the product (crop) is not a residue
                    # and if we have land use info from the IAM
                    if (
                        "farming and supply" in ds["name"].lower()
                        and crop_type.lower() in self.crops_properties
                    ):

                        # lower heating value, as received
                        lhv_ar = ds["LHV [MJ/kg dry]"] * (
                            1 - ds["Moisture content [% wt]"]
                        )

                        for exc in ds["exchanges"]:
                            # we adjust the land use
                            if exc["type"] == "biosphere" and exc["name"].startswith(
                                "Occupation"
                            ):

                                # Ha/GJ
                                land_use = (
                                    self.iam_data.data.loc[
                                        dict(
                                            region=region,
                                            variables=self.crops_properties[
                                                crop_type
                                            ],
                                        )
                                    ]
                                    .interp(year=self.year)
                                    .values
                                )
                                # HA to m2
                                land_use *= 10000
                                # m2/GJ to m2/MJ
                                land_use /= 1000

                                # m2/kg, as received
                                land_use *= lhv_ar

                                # update exchange value
                                exc["amount"] = land_use

                                string = (
                                    f"The land area occupied has been modified to {land_use}, "
                                    f"to be in line with the pathway {self.scenario} of {self.model.upper()} "
                                    f"in {self.year} in the region {region}."
                                )
                                if "comment" in ds:
                                    ds["comment"] += string
                                else:
                                    ds["comment"] = string

                    # if this is a farming activity
                    # and if the product (crop) is not a residue
                    # and if we have land use change CO2 info from the IAM
                    if (
                        "farming and supply" in ds["name"].lower()
                        and crop_type.lower() in self.crops_properties
                    ):

                        # then, we should include the Land Use Change-induced CO2 emissions
                        # those are given in kg CO2-eq./GJ of primary crop energy

                        # kg CO2/GJ
                        land_use_co2 = (
                            self.iam_data.data.loc[
                                dict(
                                    region=region,
                                    variables=self.crops_properties[crop_type]["land_use_change"][self.model],
                                )
                            ]
                            .interp(year=self.year)
                            .values
                        )

                        # lower heating value, as received
                        lhv_ar = ds["LHV [MJ/kg dry]"] * (
                            1 - ds["Moisture content [% wt]"]
                        )

                        # kg CO2/MJ
                        land_use_co2 /= 1000
                        land_use_co2 *= lhv_ar

                        land_use_co2_exc = {
                            "uncertainty type": 0,
                            "loc": land_use_co2,
                            "amount": land_use_co2,
                            "type": "biosphere",
                            "name": "Carbon dioxide, from soil or biomass stock",
                            "unit": "kilogram",
                            "input": (
                                "biosphere3",
                                "78eb1859-abd9-44c6-9ce3-f3b5b33d619c",
                            ),
                            "categories": (
                                "air",
                                "non-urban air or from high stacks",
                            ),
                        }
                        ds["exchanges"].append(land_use_co2_exc)

                        string = (
                            f"{land_use_co2} kg of land use-induced CO2 has been added by premise, "
                            f"to be in line with the pathway {self.scenario} of {self.model.upper()} "
                            f"in {self.year} in the region {region}."
                        )

                        if "comment" in ds:
                            ds["comment"] += string
                        else:
                            ds["comment"] = string

                    if (ds["name"], ds["location"]) not in added_acts:
                        added_acts.append((ds["name"], ds["location"]))
                        self.db.append(ds)

    def fetch_proxies(self, name, ref_prod, relink=False):
        """
        Fetch dataset proxies, given a dataset `name` and `reference product`.
        Store a copy for each REMIND region.
        If a REMIND region does not find a fitting ecoinvent location,
        fetch a dataset with a "RoW" location.
        Delete original datasets from the database.

        :return:
        """

        d_map = {
            self.geo.ecoinvent_to_iam_location(d["location"]): d["location"]
            for d in ws.get_many(
                self.db,
                ws.equals("name", name),
                ws.equals("reference product", ref_prod),
            )
        }

        d_iam_to_eco = {r: d_map.get(r, "RoW") for r in self.list_iam_regions}

        d_act = {}

        for d in d_iam_to_eco:
            try:
                ds = ws.get_one(
                    self.db,
                    ws.equals("name", name),
                    ws.equals("reference product", ref_prod),
                    ws.equals("location", d_iam_to_eco[d]),
                )

            except ws.NoResults:

                # trying with `GLO`
                ds = ws.get_one(
                    self.db,
                    ws.equals("name", name),
                    ws.equals("reference product", ref_prod),
                    ws.equals("location", "GLO"),
                )

            d_act[d] = wt.copy_to_new_location(ds, d)
            d_act[d]["code"] = str(uuid.uuid4().hex)

            for exc in ws.production(d_act[d]):
                if "input" in exc:
                    exc.pop("input")

            if "input" in d_act[d]:
                d_act[d].pop("input")

            if relink:
                d_act[d] = relink_technosphere_exchanges(d_act[d], self.db, self.model)

        deleted_markets = [
            (act["name"], act["reference product"], act["location"])
            for act in self.db
            if (act["name"], act["reference product"]) == (name, ref_prod)
        ]

        with open(
            DATA_DIR
            / "logs/log deleted fuel datasets {} {} {}-{}.csv".format(
                self.model, self.scenario, self.year, date.today()
            ),
            "a",
        ) as csv_file:
            writer = csv.writer(csv_file, delimiter=";", lineterminator="\n")
            for line in deleted_markets:
                writer.writerow(line)

        # Remove old datasets
        self.db = [
            act
            for act in self.db
            if (act["name"], act["reference product"]) != (name, ref_prod)
        ]

        return d_act

    def get_iam_mapping(self):
        """
        Define filter functions that decide which wurst datasets to modify.
        :return: dictionary that contains filters and functions
        :rtype: dict
        """

        return {
            fuel: {
                "find_share": self.fetch_fuel_share,
                "fuel filters": self.fuels_map[fuel],
            }
            for fuel in self.iam_data.fuel_markets.variables.values
        }

    def fetch_fuel_share(self, fuel, fuel_types, region):
        """Return a fuel mix for a specific IAM region, for a specific year."""

        vars = [
            v
            for v in self.iam_data.fuel_markets.variables.values
            if any(x.lower() in v.lower() for x in fuel_types)
        ]

        return (
            self.iam_data.fuel_markets.sel(region=region, variables=fuel)
            / self.iam_data.fuel_markets.sel(region=region, variables=vars).sum(
                dim="variables"
            )
        ).values

    def relink_activities_to_new_markets(self):
        """
        Links fuel input exchanges to new datasets with the appropriate IAM location.

        Does not return anything.
        """

        # Filter all activities that consume fuels
        acts_to_ignore = list(set([x[0] for x in list(self.new_fuel_markets.keys())]))

        for ds in ws.get_many(
            self.db,
            ws.exclude(ws.either(*[ws.contains("name", n) for n in acts_to_ignore])),
        ):

            # check that a fuel input exchange is present in the list of inputs
            if any(
                f[0] == exc["name"]
                for exc in ds["exchanges"]
                for f in self.new_fuel_markets.keys()
            ):
                amount_fossil_co2, amount_non_fossil_co2 = [0, 0]

                for name in [
                    ("market for petrol, unleaded", "petrol, unleaded", "kilogram"),
                    ("market for petrol, low-sulfur", "petrol, low-sulfur", "kilogram"),
                    ("market for diesel, low-sulfur", "diesel, low-sulfur", "kilogram"),
                    ("market for diesel", "diesel", "kilogram"),
                    (
                        "market for natural gas, high pressure",
                        "natural gas, high pressure",
                        "cubic meter",
                    ),
                    ("market for hydrogen, gaseous", "hydrogen, gaseous", "kilogram"),
                ]:

                    # checking that it is one of the markets
                    # that has been newly created
                    if name[0] in acts_to_ignore:

                        excs = list(
                            ws.get_many(
                                ds["exchanges"],
                                ws.equals("name", name[0]),
                                ws.either(
                                    *[
                                        ws.equals("unit", "kilogram"),
                                        ws.equals("unit", "cubic meter"),
                                    ]
                                ),
                                ws.equals("type", "technosphere"),
                            )
                        )

                        amount = 0
                        for exc in excs:
                            amount += exc["amount"]
                            ds["exchanges"].remove(exc)

                        if amount > 0:
                            if ds["location"] in self.list_iam_regions:
                                supplier_loc = ds["location"]

                            else:
                                new_loc = self.geo.ecoinvent_to_iam_location(
                                    ds["location"]
                                )
                                supplier_loc = (
                                    new_loc
                                    if new_loc in self.list_iam_regions
                                    else self.list_iam_regions[0]
                                )

                            new_exc = {
                                "name": name[0],
                                "product": name[1],
                                "amount": amount,
                                "type": "technosphere",
                                "unit": name[2],
                                "location": supplier_loc,
                            }

                            ds["exchanges"].append(new_exc)

                            amount_fossil_co2 += (
                                amount
                                * self.new_fuel_markets[(name[0], supplier_loc)][
                                    "fossil CO2"
                                ]
                            )
                            amount_non_fossil_co2 += (
                                amount
                                * self.new_fuel_markets[(name[0], supplier_loc)][
                                    "non-fossil CO2"
                                ]
                            )

                # update fossil and biogenic CO2 emissions
                list_items_to_ignore = [
                    "blending",
                    "market group",
                    "lubricating oil production",
                    "petrol production",
                ]
                if amount_non_fossil_co2 > 0 and not any(
                    x in ds["name"].lower() for x in list_items_to_ignore
                ):

                    # test for the presence of a fossil CO2 flow
                    if (
                        len(
                            [
                                e
                                for e in ds["exchanges"]
                                if "Carbon dioxide, fossil" in e["name"]
                            ]
                        )
                        == 0
                    ):
                        print(
                            f"{ds['name'], ds['location']} has not fossil CO2 output flow."
                        )

                    # subtract the biogenic CO2 amount to the
                    # initial fossil CO2 emission amount

                    for exc in ws.biosphere(
                        ds, ws.equals("name", "Carbon dioxide, fossil")
                    ):
                        if (exc["amount"] - amount_non_fossil_co2) < 0:
                            exc["amount"] = 0
                        else:
                            exc["amount"] -= amount_non_fossil_co2

                    # add the biogenic CO2 emission flow
                    non_fossil_co2 = {
                        "uncertainty type": 0,
                        "loc": amount_non_fossil_co2,
                        "amount": amount_non_fossil_co2,
                        "type": "biosphere",
                        "name": "Carbon dioxide, from soil or biomass stock",
                        "unit": "kilogram",
                        "categories": ("air",),
                        "input": ("biosphere3", "e4e9febc-07c1-403d-8d3a-6707bb4d96e6"),
                    }

                    ds["exchanges"].append(non_fossil_co2)

    @staticmethod
    def get_shares_from_production_volume(ds):
        """
        Return shares of supply based on production volumes
        :param ds: list of datasets
        :return: dictionary with (dataset name, dataset location) as keys, shares as values. Shares total 1.
        :rtype: dict
        """
        dict_act = {}
        total_production_volume = 0
        for act in ds:
            for exc in ws.production(act):
                dict_act[
                    (
                        act["name"],
                        act["location"],
                        act["reference product"],
                        act["unit"],
                    )
                ] = float(exc.get("production volume", 1e-3))
                total_production_volume += float(exc.get("production volume", 1e-3))

        for d in dict_act:
            dict_act[d] /= total_production_volume

        return dict_act

    def filter_results(self, item_to_look_for, results, field_to_look_at):

        return [r for r in results if item_to_look_for not in r[field_to_look_at]]

    def select_multiple_suppliers(self, fuel, d_fuels, ds, look_for):

        # We have several potential fuel suppliers
        # We will look up their respective production volumes
        # And include them proportionally to it

        possible_suppliers = list(
            ws.get_many(
                self.db,
                ws.either(
                    *[ws.equals("name", c) for c in set(d_fuels[fuel]["fuel filters"])]
                ),
                ws.either(*[ws.contains("reference product", l) for l in look_for]),
                ws.doesnt_contain_any(
                    "reference product", ["petroleum coke", "petroleum gas"]
                ),
                ws.equals("location", ds["location"]),
            )
        )

        if "low-sulfur" in ds["name"]:
            possible_suppliers = self.filter_results(
                "unleaded", possible_suppliers, "reference product"
            )

        if "unleaded" in ds["name"]:
            possible_suppliers = self.filter_results(
                "low-sulfur", possible_suppliers, "reference product"
            )

        if len(possible_suppliers) == 0:
            possible_suppliers = list(
                ws.get_many(
                    self.db,
                    ws.either(
                        *[
                            ws.equals("name", c)
                            for c in set(d_fuels[fuel]["fuel filters"])
                        ]
                    ),
                    ws.either(
                        *[
                            ws.equals("location", l)
                            for l in self.geo.iam_to_ecoinvent_location(ds["location"])
                        ]
                    ),
                    ws.either(*[ws.contains("reference product", l) for l in look_for]),
                    ws.doesnt_contain_any(
                        "reference product", ["petroleum coke", "petroleum gas"]
                    ),
                )
            )

            if "low-sulfur" in ds["name"]:
                possible_suppliers = self.filter_results(
                    "unleaded", possible_suppliers, "reference product"
                )

            if "unleaded" in ds["name"]:
                possible_suppliers = self.filter_results(
                    "low-sulfur", possible_suppliers, "reference product"
                )

        if len(possible_suppliers) == 0:
            possible_suppliers = list(
                ws.get_many(
                    self.db,
                    ws.either(
                        *[
                            ws.equals("name", c)
                            for c in set(d_fuels[fuel]["fuel filters"])
                        ]
                    ),
                    ws.equals("location", "RoW"),
                    ws.either(*[ws.contains("reference product", l) for l in look_for]),
                    ws.doesnt_contain_any(
                        "reference product", ["petroleum coke", "petroleum gas"]
                    ),
                )
            )

            if "low-sulfur" in ds["name"]:
                possible_suppliers = self.filter_results(
                    "unleaded", possible_suppliers, "reference product"
                )

            if "unleaded" in ds["name"]:
                possible_suppliers = self.filter_results(
                    "low-sulfur", possible_suppliers, "reference product"
                )

        possible_suppliers = self.get_shares_from_production_volume(possible_suppliers)

        return possible_suppliers

    def generate_fuel_supply_chains(self):
        """Duplicate fuel chains and make them IAM region-specific"""

        # DAC datasets
        print("Generate region-specific direct air capture processes.")
        self.generate_DAC_activities()

        # hydrogen
        self.generate_hydrogen_activities()

        # biogas
        print("Generate region-specific biogas and syngas supply chains.")
        self.generate_biogas_activities()

        # synthetic fuels
        print("Generate region-specific synthetic fuel supply chains.")
        self.generate_synthetic_fuel_activities()

        # biofuels
        print("Generate region-specific biofuel fuel supply chains.")
        self.generate_biofuel_activities()

    def generate_fuel_markets(self):
        """Create new fuel supply chains
        and update existing fuel markets"""

        # Create new fuel supply chains
        self.generate_fuel_supply_chains()

        print("Generate new fuel markets.")

        fuel_markets = [
            ("market for petrol, unleaded", "petrol, unleaded", "kilogram", 42.6),
            ("market for petrol, low-sulfur", "petrol, low-sulfur", "kilogram", 42.6),
            ("market for diesel, low-sulfur", "diesel, low-sulfur", "kilogram", 43),
            ("market for diesel", "diesel", "kilogram", 43),
            (
                "market for natural gas, high pressure",
                "natural gas, high pressure",
                "cubic meter",
                47.5,
            ),
            ("market for hydrogen, gaseous", "hydrogen, gaseous", "kilogram", 120),
        ]

        # refresh the fuel filters
        # as some have been created in the meanwhile
        mapping = InventorySet(self.db)
        self.fuels_map = mapping.generate_fuel_map()
        d_fuels = self.get_iam_mapping()

        # to log new fuel markets
        new_fuel_markets = [
            [
                "market name",
                "location",
                "unit",
                "reference product",
                "fuel type",
                "supplier name",
                "supplier reference product",
                "supplier location",
                "supplier unit",
                "fuel mix share (energy-wise)",
                "amount supplied [kg]",
                "LHV [mj/kg]",
                "CO2 emmission factor [kg CO2]",
                "biogenic share",
            ]
        ]

        for fuel_market in fuel_markets:

            print(f"--> {fuel_market[0]}")

            if any(fuel_market[1].split(", ")[0] in f for f in self.fuel_labels):

                d_act = self.fetch_proxies(fuel_market[0], fuel_market[1], relink=True)

                for region in d_act:

                    string = " Fuel market composition: "
                    fossil_co2, non_fossil_co2, final_lhv = [0, 0, 0]

                    ds = d_act[region]

                    # remove existing fuel providers
                    ds["exchanges"] = [
                        exc
                        for exc in ds["exchanges"]
                        if exc["type"] != "technosphere"
                        or (
                            exc["product"] != ds["reference product"]
                            and not any(
                                x in exc["name"]
                                for x in ["production", "evaporation", "import"]
                            )
                        )
                    ]

                    if "petrol" in fuel_market[0]:
                        look_for = ["petrol", "ethanol", "methanol", "gasoline"]

                    if "diesel" in fuel_market[0]:
                        look_for = ["diesel", "biodiesel"]

                    if "natural gas" in fuel_market[0]:
                        look_for = ["natural gas", "biomethane"]

                    if "hydrogen" in fuel_market[0]:
                        look_for = ["hydrogen"]

                    fuels = (f for f in d_fuels if any(x in f for x in look_for))

                    for fuel in fuels:

                        if fuel in self.fuel_labels:

                            share = d_fuels[fuel]["find_share"](fuel, look_for, region)

                            if share > 0:

                                possible_suppliers = self.select_multiple_suppliers(
                                    fuel, d_fuels, ds, look_for
                                )

                                if len(possible_suppliers) == 0:
                                    print(
                                        f"ISSUE with {fuel} in {region} for ds in location {ds['location']}"
                                    )
                                    print(d_fuels[fuel])

                                for supplier in possible_suppliers:
                                    if supplier[-1] != fuel_market[2]:
                                        conversion_factor = 0.679
                                    else:
                                        conversion_factor = 1

                                    supplier_share = (
                                        share * possible_suppliers[supplier]
                                    )

                                    # LHV of the fuel before update
                                    reference_LHV = fuel_market[3]

                                    # amount of fuel input
                                    # corrected by the LHV of the initial fuel
                                    # so that the overall composition maintains
                                    # the same average LHV

                                    amount = (
                                        supplier_share
                                        * (reference_LHV / self.fuel_properties[fuel]["lhv"])
                                        * conversion_factor
                                    )

                                    fossil_co2 += (
                                        amount
                                        * self.fuel_properties[fuel]["lhv"]
                                        * self.fuel_properties[fuel]["co2"]
                                        * (1 - self.fuel_properties[fuel]["biogenic_share"])
                                    )
                                    non_fossil_co2 += (
                                        amount
                                        * self.fuel_properties[fuel]["lhv"]
                                        * self.fuel_properties[fuel]["co2"]
                                        * self.fuel_properties[fuel]["biogenic_share"]
                                    )

                                    final_lhv += amount * self.fuel_properties[fuel]["lhv"]

                                    ds["exchanges"].append(
                                        {
                                            "uncertainty type": 0,
                                            "amount": amount,
                                            "product": supplier[2],
                                            "name": supplier[0],
                                            "unit": supplier[-1],
                                            "location": supplier[1],
                                            "type": "technosphere",
                                        }
                                    )

                                    # log
                                    new_fuel_markets.append(
                                        [
                                            ds["name"],
                                            ds["location"],
                                            ds["unit"],
                                            ds["reference product"],
                                            fuel,
                                            supplier[0],
                                            supplier[2],
                                            supplier[1],
                                            supplier[-1],
                                            share,
                                            amount,
                                            self.fuel_properties[fuel]["lhv"],
                                            self.fuel_properties[fuel]["co2"],
                                            self.fuel_properties[fuel]["biogenic_share"],
                                        ]
                                    )

                                string += f"{fuel.capitalize()}: {(share * 100):.1f} pct @ {self.fuel_properties[fuel]['lhv']} MJ/kg. "

                    string += f"Final average LHV of {final_lhv} MJ/kg."

                    if "comment" in ds:
                        ds["comment"] += string
                    else:
                        ds["comment"] = string

                    # add two new fields: `fossil CO2` and `biogenic CO2`
                    ds["fossil CO2"] = fossil_co2
                    ds["non-fossil CO2"] = non_fossil_co2
                    ds["LHV"] = final_lhv

                    # add fuel market to the dictionary
                    self.new_fuel_markets[(ds["name"], ds["location"])] = {
                        "fossil CO2": fossil_co2,
                        "non-fossil CO2": non_fossil_co2,
                        "LHV": final_lhv,
                    }

                self.db.extend([v for v in d_act.values()])

        # write log to CSV
        with open(
            DATA_DIR
            / "logs/log created fuel markets {} {} {}-{}.csv".format(
                self.model, self.scenario, self.year, date.today()
            ),
            "a",
        ) as csv_file:
            writer = csv.writer(csv_file, delimiter=";", lineterminator="\n")
            for line in new_fuel_markets:
                writer.writerow(line)

        self.relink_activities_to_new_markets()

        print("Log of deleted fuel markets saved in {}".format(DATA_DIR / "logs"))
        print("Log of created fuel markets saved in {}".format(DATA_DIR / "logs"))

        return self.db
