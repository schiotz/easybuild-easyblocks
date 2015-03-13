##
# Copyright 2009-2015 Ghent University
#
# This file is part of EasyBuild,
# originally created by the HPC team of Ghent University (http://ugent.be/hpc/en),
# with support of Ghent University (http://ugent.be/hpc),
# the Flemish Supercomputer Centre (VSC) (https://vscentrum.be/nl/en),
# the Hercules foundation (http://www.herculesstichting.be/in_English)
# and the Department of Economy, Science and Innovation (EWI) (http://www.ewi-vlaanderen.be/en).
#
# http://github.com/hpcugent/easybuild
#
# EasyBuild is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation v2.
#
# EasyBuild is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with EasyBuild.  If not, see <http://www.gnu.org/licenses/>.
##
"""
EasyBuild support for building and installing CP2K, implemented as an easyblock

@author: Stijn De Weirdt (Ghent University)
@author: Dries Verdegem (Ghent University)
@author: Kenneth Hoste (Ghent University)
@author: Pieter De Baets (Ghent University)
@author: Jens Timmerman (Ghent University)
@author: Ward Poelmans (Ghent University)
"""

import fileinput
import glob
import re
import os
import shutil
import sys
from distutils.version import LooseVersion

import easybuild.tools.toolchain as toolchain
from easybuild.framework.easyblock import EasyBlock
from easybuild.framework.easyconfig import CUSTOM
from easybuild.tools.modules import get_software_root, get_software_version
from easybuild.tools.run import run_cmd
from easybuild.tools.systemtools import get_avail_core_count

# CP2K needs this version of libxc
LIBXC_MIN_VERSION = '2.0.1'


class EB_CP2K(EasyBlock):
    """
    Support for building CP2K
    - prepare module include files if required
    - generate custom config file in 'arch' directory
    - build CP2K
    - run regression test if desired
    - install by copying binary executables
    """

    def __init__(self, *args, **kwargs):
        super(EB_CP2K, self).__init__(*args, **kwargs)

        self.typearch = None

        # this should be set to False for old versions of GCC (e.g. v4.1)
        self.compilerISO_C_BINDING = True

        # compiler options that need to be set in Makefile
        self.debug = ''
        self.fpic = ''

        self.libsmm = ''
        self.modincpath = ''
        self.openmp = ''

        self.make_instructions = ''

        # always enable testing for CP2K
        self.cfg['runtest'] = True

    @staticmethod
    def extra_options():
        extra_vars = {
            'type': ['popt', "Type of build ('popt' or 'psmp')", CUSTOM],
            'typeopt': [True, "Enable optimization", CUSTOM],
            'modincprefix': ['', "IMKL prefix for modinc include dir", CUSTOM],
            'modinc': [[], ("List of modinc's to use (*.f90], or 'True' to use "
                            "all found at given prefix"), CUSTOM],
            'extracflags': ['', "Extra CFLAGS to be added", CUSTOM],
            'extradflags': ['', "Extra DFLAGS to be added", CUSTOM],
            'ignore_regtest_fails': [False, ("Ignore failures in regression test "
                                             "(should be used with care)"), CUSTOM],
            'maxtasks': [3, ("Maximum number of CP2K instances run at "
                             "the same time during testing"), CUSTOM],
        }
        return EasyBlock.extra_options(extra_vars)

    def _generate_makefile(self, options):
        """Generate Makefile based on options dictionary and optional make instructions"""

        text = "# Makefile generated by CP2K._generateMakefile, items might appear in random order\n"
        for key, value in options.iteritems():
            text += "%s = %s\n" % (key, value)
        return text + self.make_instructions

    def configure_step(self):
        """Configure build
        - build Libint wrapper
        - generate Makefile
        """

        known_types = ['popt', 'psmp']
        if self.cfg['type'] not in known_types:
            self.log.error("Unknown build type specified: '%s', known types are %s" % (self.cfg['type'], known_types))

        # correct start dir, if needed
        # recent CP2K versions have a 'cp2k' dir in the unpacked 'cp2k' dir
        cp2k_path = os.path.join(self.cfg['start_dir'], 'cp2k')
        if os.path.exists(cp2k_path):
            self.cfg['start_dir'] = cp2k_path
            self.log.info("Corrected start_dir to %s" % self.cfg['start_dir'])

        # set compilers options according to toolchain config
        # full debug: -g -traceback -check all -fp-stack-check
        # -g links to mpi debug libs
        if self.toolchain.options['debug']:
            self.debug = '-g'
            self.log.info("Debug build")
        if self.toolchain.options['pic']:
            self.fpic = "-fPIC"
            self.log.info("Using fPIC")

        # report on extra flags being used
        if self.cfg['extracflags']:
            self.log.info("Using extra CFLAGS: %s" % self.cfg['extracflags'])
        if self.cfg['extradflags']:
            self.log.info("Using extra CFLAGS: %s" % self.cfg['extradflags'])

        # libsmm support
        libsmm = get_software_root('libsmm')
        if libsmm:
            libsmms = glob.glob(os.path.join(libsmm, 'lib', 'libsmm_*nn.a'))
            dfs = [os.path.basename(os.path.splitext(x)[0]).replace('lib', '-D__HAS_') for x in libsmms]
            moredflags = ' ' + ' '.join(dfs)
            self.cfg.update('extradflags', moredflags)
            self.libsmm = ' '.join(libsmms)
            self.log.debug('Using libsmm %s (extradflags %s)' % (self.libsmm, moredflags))

        # obtain list of modinc's to use
        if self.cfg["modinc"]:
            self.modincpath = self.prepmodinc()

        # set typearch
        self.typearch = "Linux-x86-64-%s" % self.toolchain.name

        # extra make instructions
        self.make_instructions = ''  # "graphcon.o: graphcon.F\n\t$(FC) -c $(FCFLAGS2) $<\n"

        # compiler toolchain specific configuration
        comp_fam = self.toolchain.comp_family()
        if comp_fam == toolchain.INTELCOMP:
            options = self.configure_intel_based()
        elif comp_fam == toolchain.GCC:
            options = self.configure_GCC_based()
        else:
            self.log.error("Don't know how to tweak configuration for compiler used.")

        # BLAS related
        if get_software_root('IMKL'):
            options = self.configure_MKL(options)
        elif get_software_root('ACML'):
            options = self.configure_ACML(options)
        else:
            options = self.configure_BLAS_lib(options)

        if get_software_root('FFTW'):
            options = self.configure_FFTW(options)

        if get_software_root('LAPACK'):
            options = self.configure_LAPACK(options)

        if get_software_root('ScaLAPACK'):
            options = self.configure_ScaLAPACK(options)

        # avoid group nesting
        options['LIBS'] = options['LIBS'].replace('-Wl,--start-group', '').replace('-Wl,--end-group', '')

        options['LIBS'] = "-Wl,--start-group %s -Wl,--end-group" % options['LIBS']

        # create arch file using options set
        archfile = os.path.join(self.cfg['start_dir'], 'arch',
                                '%s.%s' % (self.typearch, self.cfg['type']))
        try:
            txt = self._generate_makefile(options)
            f = open(archfile, 'w')
            f.write(txt)
            f.close()
            self.log.info("Content of makefile (%s):\n%s" % (archfile, txt))
        except IOError, err:
            self.log.error("Writing makefile %s failed: %s" % (archfile, err))

    def prepmodinc(self):
        """Prepare list of module files"""

        self.log.debug("Preparing module files")

        imkl = get_software_root('IMKL')

        if imkl:

            # prepare modinc target path
            modincpath = os.path.join(os.path.dirname(os.path.normpath(self.cfg['start_dir'])), 'modinc')
            self.log.debug("Preparing module files in %s" % modincpath)

            try:
                os.mkdir(modincpath)
            except OSError, err:
                self.log.error("Failed to create directory for module include files: %s" % err)

            # get list of modinc source files
            modincdir = os.path.join(imkl, self.cfg["modincprefix"], 'include')

            if type(self.cfg["modinc"]) == list:
                modfiles = [os.path.join(modincdir, x) for x in self.cfg["modinc"]]

            elif type(self.cfg["modinc"]) == bool and type(self.cfg["modinc"]):
                modfiles = glob.glob(os.path.join(modincdir, '*.f90'))

            else:
                self.log.error("prepmodinc: Please specify either a boolean value "
                               "or a list of files in modinc (found: %s)." % self.cfg["modinc"])

            f77 = os.getenv('F77')
            if not f77:
                self.log.error("F77 environment variable not set, can't continue.")

            # create modinc files
            for f in modfiles:
                if f77.endswith('ifort'):
                    cmd = "%s -module %s -c %s" % (f77, modincpath, f)
                elif f77 in ['gfortran', 'mpif77']:
                    cmd = "%s -J%s -c %s" % (f77, modincpath, f)
                else:
                    self.log.error("prepmodinc: Unknown value specified for F77 (%s)" % f77)

                run_cmd(cmd, log_all=True, simple=True)

            return modincpath
        else:
            self.log.error("Don't know how to prepare modinc, IMKL not found")

    def configure_common(self):
        """Common configuration for all toolchains"""

        # openmp introduces 2 major differences
        # -automatic is default: -noautomatic -auto-scalar
        # some mem-bandwidth optimisation
        if self.cfg['type'] == 'psmp':
            self.openmp = self.toolchain.get_flag('openmp')

        # determine which opt flags to use
        if self.cfg['typeopt']:
            optflags = 'OPT'
            regflags = 'OPT2'
        else:
            optflags = 'NOOPT'
            regflags = 'NOOPT'

        # make sure a MPI-2 able MPI lib is used
        mpi2 = False
        if hasattr(self.toolchain, 'MPI_FAMILY') and self.toolchain.MPI_FAMILY is not None:
            mpi_spec_by_fam = {
                toolchain.MPICH: 'mpi2',  # MPICH is MPICH v3.x, which is MPI2 compatible
                toolchain.MPICH2: 'mpi2',
                toolchain.MVAPICH2: 'mpi2',
                toolchain.OPENMPI: 'mpi2',
                toolchain.IMPI: 'mpi2',
            }
            mpi_fam = self.toolchain.mpi_family()
            mpi_spec = mpi_spec_by_fam.get(mpi_fam)
            if mpi_spec is not None:
                mpi2 = True
            self.log.debug("Determined MPI specification based on MPI toolchain component: %s" % mpi_spec)
        else:
            # can't use toolchain.mpi_family, because of dummy toolchain
            mpi2libs = ['impi', 'MVAPICH2', 'OpenMPI', 'MPICH2', 'MPICH']
            for mpi2lib in mpi2libs:
                if get_software_root(mpi2lib):
                    mpi2 = True
                    self.log.debug("Determined MPI specification based on loaded MPI module: %s")
                else:
                    self.log.debug("MPI-2 supporting MPI library %s not loaded.")
            
        if not mpi2:
            self.log.error("CP2K needs MPI-2, no known MPI-2 supporting library loaded?")

        options = {
            'CC': os.getenv('MPICC'),
            'CPP': '',
            'FC': '%s %s' % (os.getenv('MPIF90'), self.openmp),
            'LD': '%s %s' % (os.getenv('MPIF90'), self.openmp),
            'AR': 'ar -r',
            'CPPFLAGS': '',

            'FPIC': self.fpic,
            'DEBUG': self.debug,

            'FCFLAGS': '$(FCFLAGS%s)' % optflags,
            'FCFLAGS2': '$(FCFLAGS%s)' % regflags,

            'CFLAGS': ' %s %s $(FPIC) $(DEBUG) %s ' % (os.getenv('EBVARCPPFLAGS'),
                                                       os.getenv('EBVARLDFLAGS'),
                                                       self.cfg['extracflags']),
            'DFLAGS': ' -D__parallel -D__BLACS -D__SCALAPACK -D__FFTSG %s' % self.cfg['extradflags'],

            'LIBS': os.getenv('LIBS'),

            'FCFLAGSNOOPT': '$(DFLAGS) $(CFLAGS) -O0  $(FREE) $(FPIC) $(DEBUG)',
            'FCFLAGSOPT': '-O2 $(FREE) $(SAFE) $(FPIC) $(DEBUG)',
            'FCFLAGSOPT2': '-O1 $(FREE) $(SAFE) $(FPIC) $(DEBUG)'
        }

        libint = get_software_root('LibInt')
        if libint:
            options['DFLAGS'] += ' -D__LIBINT'

            libintcompiler = "%s %s" % (os.getenv('CC'), os.getenv('CFLAGS'))

            # Build libint-wrapper, if required
            libint_wrapper = ''

            # required for old versions of GCC
            if not self.compilerISO_C_BINDING:
                options['DFLAGS'] += ' -D__HAS_NO_ISO_C_BINDING'

                # determine path for libint_tools dir
                libinttools_paths = ['libint_tools', 'tools/hfx_tools/libint_tools']
                libinttools_path = None
                for path in libinttools_paths:
                    path = os.path.join(self.cfg['start_dir'], path)
                    if os.path.isdir(path):
                        libinttools_path = path
                        os.chdir(libinttools_path)
                if not libinttools_path:
                    self.log.error("No libinttools dir found")

                # build libint wrapper
                cmd = "%s -c libint_cpp_wrapper.cpp -I%s/include" % (libintcompiler, libint)
                if not run_cmd(cmd, log_all=True, simple=True):
                    self.log.error("Building the libint wrapper failed")
                libint_wrapper = '%s/libint_cpp_wrapper.o' % libinttools_path

            # determine LibInt libraries based on major version number
            libint_maj_ver = get_software_version('LibInt').split('.')[0]
            if libint_maj_ver == '1':
                libint_libs = "$(LIBINTLIB)/libderiv.a $(LIBINTLIB)/libint.a $(LIBINTLIB)/libr12.a"
            elif libint_maj_ver == '2':
                libint_libs = "$(LIBINTLIB)/libint2.a"
            else:
                self.log.error("Don't know how to handle libint version %s" % libint_maj_ver)
            self.log.info("Using LibInt version %s" % (libint_maj_ver))

            options['LIBINTLIB'] = '%s/lib' % libint
            options['LIBS'] += ' %s -lstdc++ %s' % (libint_libs, libint_wrapper)

        else:
            # throw a warning, since CP2K without LibInt doesn't make much sense
            self.log.warning("LibInt module not loaded, so building without LibInt support")

            
        libxc = get_software_root('libxc')
        if libxc:
            cur_libxc_version = get_software_version('libxc')
            if LooseVersion(cur_libxc_version) < LooseVersion(LIBXC_MIN_VERSION):
                self.log.error("CP2K only works with libxc v%s (or later)" % LIBXC_MIN_VERSION)

            options['DFLAGS'] += ' -D__LIBXC2'
            if LooseVersion(cur_libxc_version) >= LooseVersion('2.2'):
                options['LIBS'] += ' -L%s/lib -lxcf90 -lxc' % libxc
            else:
                options['LIBS'] += ' -L%s/lib -lxc' % libxc
            self.log.info("Using Libxc-%s" % cur_libxc_version)
        else:
            self.log.info("libxc module not loaded, so building without libxc support")

        return options

    def configure_intel_based(self):
        """Configure for Intel based toolchains"""

        # based on guidelines available at
        # http://software.intel.com/en-us/articles/build-cp2k-using-intel-fortran-compiler-professional-edition/
        intelurl = ''.join(["http://software.intel.com/en-us/articles/",
                            "build-cp2k-using-intel-fortran-compiler-professional-edition/"])

        options = self.configure_common()

        extrainc = ''
        if self.modincpath:
            extrainc = '-I%s' % self.modincpath

        options.update({
            # -Vaxlib : older options
            'FREE': '-fpp -free',

            # SAFE = -assume protect_parens -fp-model precise -ftz  # causes problems, so don't use this
            'SAFE': '-assume protect_parens -no-unroll-aggressive',

            'INCFLAGS': '$(DFLAGS) -I$(INTEL_INC) -I$(INTEL_INCF) %s' % extrainc,

            'LDFLAGS': '$(INCFLAGS) -i-static',
            'OBJECTS_ARCHITECTURE': 'machine_intel.o',
        })

        options['DFLAGS'] += ' -D__INTEL'

        optarch = ''
        if self.toolchain.options['optarch']:
            optarch = '-xHOST'

        options['FCFLAGSOPT'] += ' $(INCFLAGS) %s -heap-arrays 64' % optarch
        options['FCFLAGSOPT2'] += ' $(INCFLAGS) %s -heap-arrays 64' % optarch

        ifortver = LooseVersion(get_software_version('ifort'))
        failmsg = "CP2K won't build correctly with the Intel %%s compilers prior to %%s, see %s" % intelurl

        if ifortver >= LooseVersion("2011") and ifortver < LooseVersion("2012"):

            # don't allow using Intel compiler 2011 prior to release 8, because of known issue (see Intel URL)
            if ifortver >= LooseVersion("2011.8"):
                # add additional make instructions to Makefile
                self.make_instructions += "et_coupling.o: et_coupling.F\n\t$(FC) -c $(FCFLAGS2) $<\n"
                self.make_instructions += "qs_vxc_atom.o: qs_vxc_atom.F\n\t$(FC) -c $(FCFLAGS2) $<\n"

            else:
                self.log.error(failmsg % ("v12", "v2011.8"))

        elif ifortver >= LooseVersion("11"):
            if LooseVersion(get_software_version('ifort')) >= LooseVersion("11.1.072"):
                self.make_instructions += "qs_vxc_atom.o: qs_vxc_atom.F\n\t$(FC) -c $(FCFLAGS2) $<\n"

            else:
                self.log.error(failmsg % ("v11", "v11.1.072"))

        else:
            self.log.error("Intel compilers version %s not supported yet." % ifortver)

        return options

    def configure_GCC_based(self):
        """Configure for GCC based toolchains"""
        options = self.configure_common()

        options.update({
            # need this to prevent "Unterminated character constant beginning" errors
            'FREE': '-ffree-form -ffree-line-length-none',

            'LDFLAGS': '$(FCFLAGS)',
            'OBJECTS_ARCHITECTURE': 'machine_gfortran.o',
        })

        options['DFLAGS'] += ' -D__GFORTRAN'

        optarch = ''
        if self.toolchain.options['optarch']:
            optarch = '-march=native'

        options['FCFLAGSOPT'] += ' $(DFLAGS) $(CFLAGS) %s -fmax-stack-var-size=32768' % optarch
        options['FCFLAGSOPT2'] += ' $(DFLAGS) $(CFLAGS) %s' % optarch

        return options

    def configure_ACML(self, options):
        """Configure for AMD Math Core Library (ACML)"""

        openmp_suffix = ''
        if self.openmp:
            openmp_suffix = '_mp'

        options['ACML_INC'] = '%s/gfortran64%s/include' % (get_software_root('ACML'), openmp_suffix)
        options['CFLAGS'] += ' -I$(ACML_INC) -I$(FFTW_INC)'
        options['DFLAGS'] += ' -D__FFTACML'

        blas = os.getenv('LIBBLAS')
        blas = blas.replace('gfortran64', 'gfortran64%s' % openmp_suffix)
        options['LIBS'] += ' %s %s %s' % (self.libsmm, os.getenv('LIBSCALAPACK'), blas)

        return options

    def configure_BLAS_lib(self, options):
        """Configure for BLAS library."""

        options['LIBS'] += ' %s %s' % (self.libsmm, os.getenv('LIBBLAS'))

        return options

    def configure_MKL(self, options):
        """Configure for Intel Math Kernel Library (MKL)"""

        options.update({
            'INTEL_INC': '$(MKLROOT)/include',
        })

        options['DFLAGS'] += ' -D__FFTW3'

        extra = ''
        if self.modincpath:
            extra = '-I%s' % self.modincpath
        options['CFLAGS'] += ' -I$(INTEL_INC) %s $(FPIC) $(DEBUG)' % extra

        options['LIBS'] += ' %s %s' % (self.libsmm, os.getenv('LIBSCALAPACK'))

        # only use Intel FFTW wrappers if FFTW is not loaded
        if not get_software_root('FFTW'):

            options.update({
                'INTEL_INCF': '$(INTEL_INC)/fftw',
            })

            options['DFLAGS'] += ' -D__FFTMKL'

            options['CFLAGS'] += ' -I$(INTEL_INCF)'

            options['LIBS'] = '%s %s' % (os.getenv('LIBFFT'), options['LIBS'])

        return options

    def configure_FFTW(self, options):
        """Configure for Fastest Fourier Transform in the West (FFTW)"""

        fftw = get_software_root('FFTW')

        options.update({
            'FFTW_INC': '%s/include' % fftw,  # GCC
            'FFTW3INC': '%s/include' % fftw,  # Intel
            'FFTW3LIB': '%s/lib' % fftw,  # Intel
        })

        options['DFLAGS'] += ' -D__FFTW3'

        options['LIBS'] += ' -L%s -lfftw3' % os.path.join(os.getenv('EBROOTFFTW'), 'lib')

        return options

    def configure_LAPACK(self, options):
        """Configure for LAPACK library"""

        options['LIBS'] += ' %s' % os.getenv('LIBLAPACK_MT')

        return options

    def configure_ScaLAPACK(self, options):
        """Configure for ScaLAPACK library"""

        options['LIBS'] += ' %s' % os.getenv('LIBSCALAPACK')

        return options

    def build_step(self):
        """Start the actual build
        - go into makefiles dir
        - patch Makefile
        -build_and_install
        """

        makefiles = os.path.join(self.cfg['start_dir'], 'makefiles')
        try:
            os.chdir(makefiles)
        except:
            self.log.error("Can't change to makefiles dir %s: %s" % (makefiles))

        # modify makefile for parallel build
        parallel = self.cfg['parallel']
        if parallel:

            try:
                for line in fileinput.input('Makefile', inplace=1, backup='.orig.patchictce'):
                    line = re.sub(r"^PMAKE\s*=.*$", "PMAKE\t= $(SMAKE) -j %s" % parallel, line)
                    sys.stdout.write(line)
            except IOError, err:
                self.log.error("Can't modify/write Makefile in %s: %s" % (makefiles, err))

        # update make options with MAKE
        self.cfg.update('buildopts', 'MAKE="make -j %s" all' % self.cfg['parallel'])

        # update make options with ARCH and VERSION
        self.cfg.update('buildopts', 'ARCH=%s VERSION=%s' % (self.typearch, self.cfg['type']))

        cmd = "make %s" % self.cfg['buildopts']

        # clean first
        run_cmd(cmd + " clean", log_all=True, simple=True, log_output=True)

        #build_and_install
        run_cmd(cmd, log_all=True, simple=True, log_output=True)

    def test_step(self):
        """Run regression test."""

        if self.cfg['runtest']:

            # change to root of build dir
            try:
                os.chdir(self.builddir)
            except OSError, err:
                self.log.error("Failed to change to %s: %s" % self.builddir)

            # use regression test reference output if available
            # try and find an unpacked directory that starts with 'LAST-'
            regtest_refdir = None
            for d in os.listdir(self.builddir):
                if d.startswith("LAST-"):
                    regtest_refdir = d
                    break

            # location of do_regtest script
            cfg_fn = "cp2k_regtest.cfg"
            regtest_script = os.path.join(self.cfg['start_dir'], 'tools', 'regtesting', 'do_regtest')
            regtest_cmd = "%s -nosvn -nobuild -config %s" % (regtest_script, cfg_fn)
            # older version of CP2K
            if not os.path.exists(regtest_script):
                regtest_script = os.path.join(self.cfg['start_dir'], 'tools', 'do_regtest')
                regtest_cmd = "%s -nocvs -quick -nocompile -config %s" % (regtest_script, cfg_fn)

            # patch do_regtest so that reference output is used
            if regtest_refdir:
                self.log.info("Using reference output available in %s" % regtest_refdir)
                try:
                    for line in fileinput.input(regtest_script, inplace=1, backup='.orig.refout'):
                        line = re.sub(r"^(dir_last\s*=\${dir_base})/.*$", r"\1/%s" % regtest_refdir, line)
                        sys.stdout.write(line)
                except IOError, err:
                    self.log.error("Failed to modify '%s': %s" % (regtest_script, err))

            else:
                self.log.info("No reference output found for regression test, just continuing without it...")

            test_core_cnt = min(self.cfg.get('parallel', sys.maxint), 2)
            if get_avail_core_count() < test_core_cnt:
                self.log.error("Cannot run MPI tests as not enough cores (< %s) are available" % test_core_cnt)
            else:
                self.log.info("Using %s cores for the MPI tests" % test_core_cnt)

            # configure regression test
            cfg_txt = '\n'.join([
                'FORT_C_NAME="%(f90)s"',
                'dir_base=%(base)s',
                'cp2k_version=%(cp2k_version)s',
                'dir_triplet=%(triplet)s',
                'export ARCH=${dir_triplet}',
                'cp2k_dir=%(cp2k_dir)s',
                'leakcheck="YES"',
                'maxtasks=%(maxtasks)s',
                'cp2k_run_prefix="%(mpicmd_prefix)s"',
            ]) % {
                'f90': os.getenv('F90'),
                'base': os.path.dirname(os.path.normpath(self.cfg['start_dir'])),
                'cp2k_version': self.cfg['type'],
                'triplet': self.typearch,
                'cp2k_dir': os.path.basename(os.path.normpath(self.cfg['start_dir'])),
                'maxtasks': self.cfg['maxtasks'],
                'mpicmd_prefix': self.toolchain.mpi_cmd_for('', test_core_cnt),
            }

            try:
                f = open(cfg_fn, "w")
                f.write(cfg_txt)
                f.close()
            except IOError, err:
                self.log.error("Failed to create config file %s: %s" % (cfg_fn, err))

            self.log.debug("Contents of %s: %s" % (cfg_fn, cfg_txt))

            # run regression test
            (regtest_output, ec) = run_cmd(regtest_cmd, log_all=True, simple=False, log_output=True)

            if ec == 0:
                self.log.info("Regression test output:\n%s" % regtest_output)
            else:
                self.log.error("Regression test failed (non-zero exit code): %s" % regtest_output)

            # pattern to search for regression test summary
            re_pattern = "number\s+of\s+%s\s+tests\s+(?P<cnt>[0-9]+)"

            # find total number of tests
            regexp = re.compile(re_pattern % "", re.M | re.I)
            res = regexp.search(regtest_output)
            tot_cnt = None
            if res:
                tot_cnt = int(res.group('cnt'))
            else:
                self.log.error("Finding total number of tests in regression test summary failed")
            msg = "Regression test reported %%s / %s %%s tests" % tot_cnt

            # function to report on regtest results
            def test_report(test_result):
                """Report on tests with given result."""

                postmsg = ''

                test_result = test_result.upper()
                regexp = re.compile(re_pattern % test_result, re.M | re.I)

                cnt = None
                res = regexp.search(regtest_output)
                if not res:
                    self.log.error("Finding number of %s tests in regression test summary failed" % test_result.lower())
                else:
                    cnt = int(res.group('cnt'))

                logmsg = msg % (cnt, test_result.lower())

                # failed tests indicate problem with installation
                # wrong tests are only an issue when there are excessively many
                if (test_result == "FAILED" and cnt > 0) or (test_result == "WRONG" and (cnt / tot_cnt) > 0.1):
                    if self.cfg['ignore_regtest_fails']:
                        self.log.warning(logmsg)
                        self.log.info("Ignoring failures in regression test, as requested.")
                    else:
                        self.log.error(logmsg)
                elif test_result == "CORRECT" or cnt == 0:
                    self.log.info(logmsg)
                else:
                    self.log.warning(logmsg)

                return postmsg

            # number of failed/wrong tests, will report error if count is positive
            self.postmsg += test_report("FAILED")
            self.postmsg += test_report("WRONG")

            # number of new tests, will be high if a non-suitable regtest reference was used
            # will report error if count is positive (is that what we want?)
            self.postmsg += test_report("NEW")

            # number of correct tests: just report
            test_report("CORRECT")

    def install_step(self):
        """Install built CP2K
        - copy from exe to bin
        - copy tests
        """

        # copy executables
        targetdir = os.path.join(self.installdir, 'bin')
        exedir = os.path.join(self.cfg['start_dir'], 'exe/%s' % self.typearch)
        try:
            if not os.path.exists(targetdir):
                os.makedirs(targetdir)
            os.chdir(exedir)
            for exefile in os.listdir(exedir):
                if os.path.isfile(exefile):
                    shutil.copy2(exefile, targetdir)
        except OSError, err:
            self.log.error("Copying executables from %s to bin dir %s failed: %s" % (exedir, targetdir, err))

        # copy tests
        srctests = os.path.join(self.cfg['start_dir'], 'tests')
        targetdir = os.path.join(self.installdir, 'tests')
        if os.path.exists(targetdir):
            self.log.info("Won't copy tests. Destination directory %s already exists" % targetdir)
        else:
            try:
                shutil.copytree(srctests, targetdir)
            except:
                self.log.error("Copying tests from %s to %s failed" % (srctests, targetdir))

        # copy regression test results
        if self.cfg['runtest']:
            try:
                testdir = os.path.dirname(os.path.normpath(self.cfg['start_dir']))
                for d in os.listdir(testdir):
                    if d.startswith('TEST-%s-%s' % (self.typearch, self.cfg['type'])):
                        path = os.path.join(testdir, d)
                        target = os.path.join(self.installdir, d)
                        shutil.copytree(path, target)
                        self.log.info("Regression test results dir %s copied to %s" % (d, self.installdir))
                        break
            except (OSError, IOError), err:
                self.log.error("Failed to copy regression test results dir: %s" % err)

    def sanity_check_step(self):
        """Custom sanity check for CP2K"""

        cp2k_type = self.cfg['type']
        custom_paths = {
            'files': ["bin/%s.%s" % (x, cp2k_type) for x in ["cp2k", "cp2k_shell"]],
            'dirs': ["tests"]
        }

        super(EB_CP2K, self).sanity_check_step(custom_paths=custom_paths)
