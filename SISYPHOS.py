import glob
import math
import os
import shutil
import time
from typing import Dict, List, Union

import olex
import olex_core
import olx
from olexFunctions import OlexFunctions
from PluginTools import PluginTools as PT
from sisy_jobs import SisyphosBenchmarkFile

OV = OlexFunctions()


debug = bool(OV.GetParam("olex2.debug", False))
instance_path = OV.DataDir()

try:
    from_outside = False
    p_path = os.path.dirname(os.path.abspath(__file__))
except:
    from_outside = True
    p_path = os.path.dirname(os.path.abspath("__file__"))

l = open(os.path.join(p_path, "def.txt")).readlines()
d = {}
for line in l:
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    d[line.split("=")[0].strip()] = line.split("=")[1].strip()

p_name = d["p_name"]
p_htm = d["p_htm"]
p_img = eval(d["p_img"])
p_scope = d["p_scope"]

OV.SetVar("SISYPHOS_plugin_path", p_path)


class BenchJob:
    def __init__(
        self,
        base_work_path,
        id: int,
        use_nos2=False,
        nos2_params: Union[Dict, None] = None,
    ) -> None:
        if nos2_params is None:
            use_nos2 = False

        self.base_work_path = base_work_path
        self.id = id
        self.use_nos2 = use_nos2
        self.nos2_params = nos2_params

        self.work_path = os.path.join(self.base_work_path, f"job_{self.id}")
        self.time_needed = 0.0
        self.cycles_needed = 0

        self.setup_workspace()

    def __str__(self) -> str:
        res = "Job with following options:\n"
        if self.nos2_params is None:
            res += "IAM Job\n"
        else:
            for key in self.nos2_params:
                res += f"{key}:{self.nos2_params[key]}|"
        return res

    def __repr__(self) -> str:
        return f"<FAPJob Object {self.id}>"

    def setup_workspace(self) -> None:
        """Sets up the workspace for the job"""
        if not os.path.exists(self.work_path):
            os.makedirs(self.work_path)
        else:
            shutil.rmtree(self.work_path)
            os.makedirs(self.work_path)

        data_dir = os.path.dirname(self.base_work_path)
        shutil.copy(glob.glob(os.path.join(data_dir, "*.hkl"))[0], self.work_path)
        shutil.copy(glob.glob(os.path.join(data_dir, "*.ins"))[0], self.work_path)
        self.ins_file = glob.glob(os.path.join(self.work_path, "*.ins"))[0]

    def write_log(self, out: str) -> None:
        """Writes a log file to the work path"""
        with open(os.path.join(self.work_path, "out.log"), "a") as log_file:
            log_file.write(out + "\n")
            log_file.flush()
            os.fsync(log_file.fileno())

    def write_log_header(self, out: str) -> None:
        bar = "=" * 27
        self.write_log(f"{bar} {out} {bar}")

    def configure_ORCA(self) -> None:
        olx.xf.EndUpdate()
        if OV.HasGUI():
            olx.Refresh()

        # ORCA has to be over here, to allow SALTED to replace ORCA as refinement engine
        OV.SetParam("snum.NoSpherA2.source", "ORCA 6.0")
        for key in self.nos2_params:
            OV.SetParam(f"snum.NoSpherA2.{key}", f"{self.nos2_params[key]}")
            self.write_log(f"{key}: {OV.GetParam(f'snum.NoSpherA2.{key}')}")

        OV.SetParam("snum.NoSpherA2.Calculate", True)
        OV.SetParam("snum.NoSpherA2.precise_output", True)
        OV.SetParam("snum.NoSpherA2.use_aspherical", True)
        OV.SetParam("snum.NoSpherA2.h_aniso", True)
        if OV.GetParam("snum.NoSpherA2.multiplicity") == "0":
            self.write_log("I wil set a Multiplicity of 1, since none selected")
            OV.SetParam("snum.NoSpherA2.multiplicity", "1")

    def refine(self) -> None:
        """Runs the refinement using predefined settings"""
        try:
            olex.m(f"reap {self.ins_file}")
            self.write_log_header("Starting New Refinement")
            self.write_log(f"ID: {self.id}")
            self.write_log(f"Was able to load .ins: {self.ins_file}")

            olx.AddIns("EXTI")
            olx.AddIns("ACTA")

            # Fix dispersion, no idea if this is neccecary or good
            olex.m("fix disp -c")

            # Check if a weightening scheme should be used
            if OV.GetParam("sisyphos.update_weight"):
                OV.SetParam("snum.refinement.update_weight", True)
                self.write_log("Refining the weighting scheme")
            else:
                OV.SetParam("snum.refinement.update_weight", False)
                self.write_log("keeping weighting scheme")

            # Set the refinement program to G-N
            olex.m("spy.set_refinement_program(olex2.refine, Gauss-Newton)")
            self.write_log("Set refinement engine olex2.refine with G-N")

            # Perform a first refinement using IAM
            OV.SetParam("snum.NoSpherA2.use_aspherical", False)
            for _ in range(3):
                olex.m("refine 5")

            # If extinction is present, set the refinement program to Levenberg-Marquardt
            exti = olx.xf.rm.Exti()
            self.write_log(f"Found Extinction: {exti}")
            if exti != "n/a":
                if float(exti.split("(")[0]) < 0.001:
                    olex.m("delins EXTI")
                    self.write_log(f"Deleted EXTI with exti of: {exti}")
                else:
                    self.write_log("Exti > 0.001, set L-M instead of G-N")
                    olex.m(
                        "spy.set_refinement_program(olex2.refine, Levenberg-Marquardt)"
                    )

            # Perform a second refinement using IAM
            olex.m("refine 10")

            # Enable NoSpherA2 is selected
            if self.use_nos2:
                # Those two do not seem to work
                # olex.m("neutronhdist")
                # self.write_log("H atoms placed to neutron distances (NeutronHDist command)")
                self.configure_ORCA()
                self.write_log("Starting iterative NoSpherA2 refinement")
                start = time.perf_counter()
                olex.m("refine 20")
                end = time.perf_counter()
                self.write_log(f"Refinement took {end-start} seconds")
                self.write_log(f"Refinement took {OV.GetVar('Run_number')} cycles")
                self.time_needed = end - start
                self.cycles_needed = OV.GetVar("Run_number")

        except Exception as error:
            self.write_log(str(error))
            self.write_log("Failed during refinenement!")

    def parse_cif(self, loc: str) -> dict:
        """Parses the cif given by loc and returns a dictionary of parsed information

        Args:
            loc (str): Path to the .cif file to be analyzed

        Returns:
            dict: Result dictionary from cif
        """
        dat_names = [
            "mu",
            "wavelength",
            "F000",
            "tot_reflIns",
            "goof",
            "R_all",
            "R1",
            "wR2",
            "last Shift",
        ]

        corr_filts = [
            "exptl_absorpt_coefficient_mu",
            "diffrn_radiation_wavelength",
            "exptl_crystal_F_000",
            "diffrn_reflns_number",
            "refine_ls_goodness_of_fit_ref",
            "refine_ls_R_factor_all",
            "refine_ls_R_factor_gt",
            "refine_ls_wR_factor_ref",
            "REM Shift_max",
        ]
        out = {}
        try:
            with open(loc, "r") as incif:
                for line in incif:
                    for i, filter in enumerate(corr_filts):
                        if filter in line:
                            out[f"{dat_names[i]}"] = float(line.split()[-1])
            self.write_log("Basic cif extraction succesfull :)")
        except:
            self.write_log("Basic cif extraction failed! :(")
        try:
            with open(loc, "r") as incif:
                switch2 = False
                for line in incif:
                    if line.startswith("  _atom_site_refinement_flags_occupancy"):
                        switch2 = True
                        continue
                    if switch2:
                        if line.startswith("\n"):
                            switch2 = False
                        else:
                            lin = line.split(" ")
                            atom = lin[1]
                            ueq = lin[6].split("(")[0]
                            ueq_delta = lin[6].split("(")[1][:-1]
                            out[f"{atom}_ueq"] = (float(ueq), int(ueq_delta))
        except Exception as e:
            self.write_log(str(e))
            self.write_log("Extended cif extraction failed!")
        return out

    def run(self) -> None:
        self.refine()
        self.write_log_header("Finished Refinement")
        done = True
        try:
            self.extract_info()
        except:
            print("Faield to extract information")
            self.write_log("Failed to extract information!")
            done = False

        if done:
            self.write_log("Extracted Information")
            # Write a empty file called done to the work path
            with open(os.path.join(self.work_path, "done"), "w") as _:
                pass

    def extract_info(self) -> None:
        try:
            cell_stats = {}
            for x in ["a", "b", "c", "alpha", "beta", "gamma"]:
                val = olx.xf.uc.CellEx(x)
                cell_stats[x] = val
            cell_stats["volume"] = olx.xf.uc.VolumeEx()
            cell_stats["Z"] = olx.xf.au.GetZ()
            cell_stats["Zprime"] = olx.xf.au.GetZprime()
        except Exception as error:
            self.write_log(str(error))
            self.write_log("Failed to extract Cell stats.")
            pass

        hkl_stats = olex_core.GetHklStat()

        try:
            locat = glob.glob(os.path.join(self.work_path, "*.cif"))[0]
            cif_stats = self.parse_cif(locat)
        except Exception as error:
            self.write_log(str(error))
            self.write_log("Failed to extract cif stats!")
            pass

        dist_stats = {}
        dist_errs = {}
        R1_all = 0.0
        R1_gt = 0.0
        wR2 = 0.0

        try:
            # This Block will extract the bondlengths from all bonded atoms
            table_name = ""
            if self.use_nos2:
                table_name = str(OV.GetParam("snum.NoSpherA2.file"))
            from cctbx.array_family import flex
            from cctbx.crystal import calculate_distances

            # Even though never used we need this import since it initializes things we need later on
            from olexex import OlexRefinementModel
            from refinement import FullMatrixRefine
            from scitbx import matrix

            # This creates the FMR with normal equations that carries EVERYTHING!
            fmr = FullMatrixRefine()
            if table_name != "":
                # Do not run refinement, simply prepare equations
                norm_eq = fmr.run(build_only=True, table_file_name=table_name)
            else:
                norm_eq = fmr.run(build_only=True)
            # and build them
            norm_eq.build_up(False)
            R1_all = norm_eq.r1_factor()[0]
            R1_gt = norm_eq.r1_factor(cutoff_factor=2.0)[0]
            wR2 = norm_eq.wR2()

            connectivity_full = fmr.reparametrisation.connectivity_table
            xs = fmr.xray_structure()

            cell_params = fmr.olx_atoms.getCell()
            cell_errors = fmr.olx_atoms.getCellErrors()
            cell_vcv = flex.pow2(matrix.diag(cell_errors).as_flex_double_matrix())
            for i in range(3):
                for j in range(i + 1, 3):
                    if (
                        cell_params[i] == cell_params[j]
                        and cell_errors[i] == cell_errors[j]
                        and cell_params[i + 3] == 90
                        and cell_errors[i + 3] == 0
                        and cell_params[j + 3] == 90
                        and cell_errors[j + 3] == 0
                    ):
                        cell_vcv[i, j] = math.pow(cell_errors[i], 2)
                        cell_vcv[j, i] = math.pow(cell_errors[i], 2)
            # Prepare the Cell Variance covariance matrix, since we need it for error propagation in distances
            cell_vcv = cell_vcv.matrix_symmetric_as_packed_u()
            sl = xs.scatterers().extract_labels()
            sf = xs.sites_frac()
            # This is VCV from refinement equations
            cm = norm_eq.covariance_matrix_and_annotations().matrix
            pm = xs.parameter_map()
            pat = connectivity_full.pair_asu_table

            # calculate the distances using the prepared information
            distances = calculate_distances(
                pat,
                sf,
                covariance_matrix=cm,
                cell_covariance_matrix=cell_vcv,
                parameter_map=pm,
            )

            # The distances only exist once we iterate over them! Therefore build them and save them in this loop
            for i, d in enumerate(distances):
                bond = sl[d.i_seq] + "-" + sl[d.j_seq]
                dist_stats[bond] = distances.distances[i]
                dist_errs[bond] = math.sqrt(distances.variances[i])

        except Exception as error:
            print(error)
            print("Could not obtain cctbx object and calculate ESDs!\n")
            self.write_log(str(error))
            self.write_log("Failed to extract distances")
            pass

        # Write the results to a file
        with open(os.path.join(self.work_path, f"results.txt"), "a") as out:
            out.write("NoSpherA2_Dict:\n")
            if self.use_nos2:
                for key in self.nos2_params:
                    out.write(str(key) + ":" + str(self.nos2_params[key]) + ",")

            out.write("\nStats-GetHklStat:\n")
            for key in hkl_stats:
                out.write(str(key) + ":" + str(hkl_stats[key]) + ",")
            out.write("\nCell-Stats:\n")
            for key in cell_stats:
                out.write(str(key) + ":" + str(cell_stats[key]) + ",")
            out.write("\nCIF-stats:\n")
            for key in cif_stats:
                out.write(str(key) + ":" + str(cif_stats[key]) + ",")
            out.write("\nrefine_dict:\n")
            for key in [
                "max_peak",
                "max_hole",
                "res_rms",
                "goof",
                "max_shift_over_esd",
                "hooft_str",
            ]:
                out.write(
                    str(key) + ":" + str(OV.GetParam("snum.refinement." + key)) + ","
                )
            out.write(
                "R1_all:"
                + str(R1_all)
                + ",R1_gt:"
                + str(R1_gt)
                + ",wR2:"
                + str(wR2)
                + ",cycles:"
                + str(self.cycles_needed)
                + ",time:"
                + str(self.time_needed)
                + ","
            )
            out.write("\nbondlengths:\n")
            for key in dist_stats:
                out.write(str(key) + ":" + str(dist_stats[key]) + ",")
            out.write("\nbonderrors:\n")
            for key in dist_stats:
                out.write(str(key) + ":" + str(dist_errs[key]) + ",")
            out.write("\nWeight:" + str(OV.GetParam("sisyphos.update_weight")))
            out.write(f"\nNr. NPD:{olx.xf.au.NPDCount()}")
            out.write("\n+++++++++++++++++++\n")


nos_params = [
    "basis_name",
    "method",
    "ncpus",
    "mem",
    "charge",
    "multiplicity",
    "full_HAR",
    "Max_HAR_Cycles",
    "becke_accuracy",
    "Relativistic",
    "h_aniso",
    "h_afix",
    "add_disp",
    "cluster_radius",
    "DIIS",
    "cluster_grow",
    "ORCA_SCF_Conv",
    "ORCA_SCF_Strategy",
    "ORCA_Solvation",
    "pySCF_Damping",
    "ORCA_DAMP",
]


class SISYPHOS(PT):
    """SISYPHOS class for handling data processing and analysis."""

    def __init__(self):
        """Initialize SISYPHOS object."""
        super(SISYPHOS, self).__init__()
        self.p_name = p_name
        self.p_path = p_path
        self.p_scope = p_scope
        self.p_htm = p_htm
        self.p_img = p_img
        self.deal_with_phil(operation="read")
        self.print_version_date()
        if not from_outside:
            self.setup_gui()

        OV.registerFunction(self.setBasePath, True, "SISYPHOS")
        OV.registerFunction(self.setWorkPath, True, "SISYPHOS")
        OV.registerFunction(self.run, True, "SISYPHOS")
        self.base_path = os.getenv("SISYPHOS_base_path")
        self.work_path = os.getenv("SISYPHOS_work_path")

        # Get the index of the job to be processed, if not set, use -1 to indicate that all jobs should be processed
        self.sisy_job_idx = int(os.getenv("SISYPHOS_job_idx", default=-1))

        self.nos2_options = {}
        self.use_nosphera2 = False

    def setBasePath(self) -> None:
        """Select Directory of the hkl and ins file which should be processed.


        Raises:
                None
        """
        out = olex.f('choosedir("Choose your hkl and ins folder")')
        if out == " ":
            print("No directory choosen!")
        else:
            self.base_path = out
            print(f"Your data lies at:\n{out}")
        OV.SetParam("sisyphos.base_path", out)

    def setWorkPath(self) -> None:
        """Choose the directory where the benchmark jobs should be saved. This directory should also contain the benchmark file.
        Returns:
                list: List of FAPJob objects

        """
        out = olex.f('choosedir("Choose your benchmark folder")')
        if out == " ":
            print("No directory choosen!")
        else:
            self.work_path = out
            print(f"Your benchmark data lies at:\n{out}")
        OV.SetParam("sisyphos.work_path", out)

    def init_sisy_jobs(self) -> list:
        sisy_jobs = SisyphosBenchmarkFile(self.work_path)

        # Check if only one job is specified (happens mostly when running in headless mode)
        if self.sisy_job_idx != -1:  #
            if sisy_jobs.is_finished(self.sisy_job_idx):
                print(f"Job {self.sisy_job_idx} already finished, skipping")
                return []

            tmp_job_dict = self.nos2_options.copy()
            tmp_job_dict.update(sisy_jobs[self.sisy_job_idx])
            # Check if a IAM job should be performed
            if "IAM" in tmp_job_dict:
                return [BenchJob(self.work_path, self.sisy_job_idx)]
            return [
                BenchJob(
                    self.work_path, self.sisy_job_idx, self.use_nosphera2, tmp_job_dict
                )
            ]

        job_list = []
        for i, job in enumerate(sisy_jobs):
            if sisy_jobs.is_finished(i):
                print(f"Job {i} already finished, skipping")
                continue

            tmp_job_dict = self.nos2_options.copy()
            tmp_job_dict.update(job)

            if "IAM" in tmp_job_dict:
                job_list.append(BenchJob(self.work_path, i))
            else:
                job_list.append(
                    BenchJob(self.work_path, i, self.use_nosphera2, tmp_job_dict)
                )

        return job_list

    def run(self) -> None:
        """Run a refinement, using the sisyphos plugin.

        Args:
            None

        Returns:
            None
        """
        # Now that NoSpherA2 should have been initialized, we can set the parameters
        self.use_nosphera2 = OV.GetParam("sisyphos.use_nos2")
        for param in nos_params:
            self.nos2_options[param] = OV.GetParam(f"snum.NoSpherA2.{param}")

        self.nos2_options["full_HAR"] = True
        self.nos2_options["Max_HAR_Cycles"] = 15

        job_list = self.init_sisy_jobs()

        for job in job_list:
            print(
                f"--------------------------Running job {job.id}-----------------------------"
            )
            job.run()


SISYPHOS_instance = SISYPHOS()
