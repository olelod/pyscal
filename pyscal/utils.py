"""Utility function for pyscal
"""
from __future__ import absolute_import

import logging
import six

import numpy as np
import pandas as pd
from scipy.interpolate import interp1d

import pyscal
from .constants import SWINTEGERS
from .constants import EPSILON as epsilon


logging.basicConfig()
logger = logging.getLogger(__name__)


def df2str(
    dframe,
    digits=7,
    roundlevel=9,
    header=False,
    monotonocity=None,
    monotone_column=None,
    monotone_direction=None,
):
    """
    Make a string representation of a dataframe with
    proper rounding.

    This is used to print the tables in the SWOF/SGOF include files,
    explicit rounding is necessary to avoid monotonocity errors
    from truncation. Examples in test code.

    Capillary pressure must be strictly monotone if nonzero, and if a
    column name is provided, the string representation of that column is
    ensured to be strictly monotone decreasing

    Args:
        dframe (pd.DataFrame): A dataframe to print, all columns
            are included
        digits (int): Number of digits used in floating point format f.ex ".7f"
            It is not recommended to deviate from the default 7 uncritically
            for pyscal output, other code have to be tuned to ensure
            numerical robustness to the deviation.
        roundlevel (int): To how many digits should we round prior to print.
            Recommended to be > digits + 1, see test code.
        header (bool): If the dataframe column header should be included
        monotonocity (dict): Settings for monotonocity in output. A dict
            with column names as keys, with values being a dict with keys
            "sign" (-1 or +1 integer) for direction,  "upper" and "lower" for
            lower and upper limits (non-strict monotonocity is allowed at
            these upper and lower limits).
        monotone_columns (list of str): column names for which strict
            monotonocity must be preserved in output. Deprecated.
        monotone_directions (list of str): Direction of monotonocity, increasing
            or decreasing, allowed values are '-1', '1', 'inc' or 'dec'. If
            multiple columns, specify for each. Deprecated.
    """
    float_format = "%1." + str(digits) + "f"

    if monotonocity is not None and monotone_column is not None:
        raise ValueError("Do not mix new and deprecated API")

    if monotonocity is None and monotone_column is not None:
        logger.warning("monotone_column is deprecated, use monotonocity")
        monotonocity = remap_deprecated_monotonocity(
            monotone_column, monotone_direction
        )

    if monotonocity is not None:
        dframe = modify_dframe_monotonocity(dframe, monotonocity, digits)

    return dframe.round(roundlevel).to_csv(
        sep=" ", float_format=float_format, header=header, index=False
    )


def modify_dframe_monotonocity(dframe, monotonocity, digits):
    """Modify a dataframe for monotonicity.

    Columns in the dataframe are modified in-place.

    Number intervals to consider when enforcing monotonocity::

      <value>                          <orig>    <fixed>
      <lower limit>                     0.00      0.00
      <values smaller than accuracy>    0.0002    0.00
      <accuracy limit>                  0.01      0.01
      <potential constants>             0.010001  0.02
      <allow ups/downs below accuracy>  0.0100001 0.03
                                        0.01      0.04
      <upper limit minus accuracy>      0.99      0.99
      <values too close to upper limit> 0.999     1.00
      <overshooting values>             1.0001    1.00
      <upper limit>                     1.00      1.00

    Values close to  upper or lower limits (if limits are
    supplied), but which deviate from the limit by less
    than the requested accuracy are allowed, and will be
    shifted to the limits.

    Only strict monotonocity is supported. Non-strict
    monotonicity is only allowed at upper and lower limit, or
    for all-zero vectors if that option is activated.

    For non-strict monotocity, see the function clip_accumulate()

    Args:
        dframe (pd.DataFrame): Data to modify.
        monotonocity (dict): see df2str() for syntax.
        digits (int): Number of digits to ensure monotonocity for.
    """
    validate_monotonocity_arg(monotonocity, dframe.columns)

    # Wateroil.SWOF() (and similar) supply a column view
    # of the internal wateroil.table dataframe. When asked
    # to enforce monotonocity, it must be done on a copy, both
    # for speed and for not compromising the original data.

    # Round to an accuracy one notch finer than end results,
    # to avoid representation errors:
    dframe = dframe.round(digits + 1)

    # Prepare and check columns:
    for col in monotonocity:
        if dframe[col].dtype != np.float64:
            dframe.loc[:, col] = dframe[col].astype(float)

        # Bail on clearly erroneous data:
        check_almost_monotone(dframe[col], digits, monotonocity[col]["sign"])

        check_limits(dframe[col], monotonocity[col])

    # Modify data for monotonocity:
    for col in monotonocity:
        sign = monotonocity[col]["sign"]

        accuracy = 1.0 / 10.0 ** digits - epsilon

        if "allowzero" in monotonocity[col]:
            # Treat zero as an exception for strict monotonocity:
            max_value = dframe[col].abs().max()
            if max_value < accuracy and monotonocity[col]["allowzero"]:
                continue

        constants = rows_to_be_fixed(dframe[col], monotonocity[col], digits, sign)
        iterations = 0
        while constants.any():
            iterations += 1
            if iterations > 2 * len(dframe[col]):
                raise Exception("Too many iterations for monotonocity fix")

            dframe.loc[constants, col] = (
                dframe.loc[constants, col] + sign / 10.0 ** digits - epsilon
            )

            # Ensure nonstrict monotonocity and clips after each modification:
            dframe[col] = clip_accumulate(dframe[col], monotonocity[col])

            # Evaluate what is left to fix:
            constants = rows_to_be_fixed(dframe[col], monotonocity[col], digits, sign)

        # Warn if more iterations than 5% of the rows
        # (number of iterations do not necessarily correspond with
        # number of changed rows)
        if float(iterations) / float(len(dframe[col])) > 0.05:
            logger.warning(
                "Needed %s iterations on column %s of length %s",
                str(iterations),
                col,
                str(len(dframe[col])),
            )

        # Check result for monotonocity:
        # Is this possible when rows_to_be_fixed returns none??
        allowance = 1.0 / 10.0 ** digits
        if sign > 0:
            if (dframe[col].round(digits).diff() < -allowance).any():
                raise ValueError("Not possible to make colum monotonically increasing")
        else:
            if (dframe[col].round(digits).diff() > allowance).any():
                raise ValueError("Not possible to make colum monotonically decreasing")
    return dframe


def clip_accumulate(series, monotonocity):
    """
    Modify a series (vector of numbers) for non-strict monotonocity, and
    optionally clip at lower and upper limits.

    Args:
        series (pd.Series or np.array): Vector of numbers to modify
        monotonocity (dict): Monotonocity options. The keys 'lower' and 'upper'
            can be provided for clipping the vector.

    Returns:
        np.array, copy of original.
    """
    if monotonocity["sign"] > 0:
        series = np.maximum.accumulate(series)
    else:
        series = np.minimum.accumulate(series)
    if "lower" in monotonocity and "upper" in monotonocity:
        series.clip(
            lower=monotonocity["lower"], upper=monotonocity["upper"], inplace=True
        )
    elif "lower" in monotonocity:
        series.clip(lower=monotonocity["lower"], inplace=True)
    elif "upper" in monotonocity:
        series.clip(upper=monotonocity["upper"], inplace=True)
    return series


def check_limits(series, monotonocity, colname=""):
    """
    Check a series whether it obeys numerical limits.
    Equivalence to limits is allowed.

    Exceptions will be raised in case of error. Nothing is returned
    when everything is ok.

    Args:
        series (pd.Series): Vector of numbers to check
        monotonocity (dict): Keys 'upper' and 'lower' are optional
            and point to numerical limits.
    Returns:
        None
    """
    if series.empty:
        return
    if "upper" in monotonocity and (series > monotonocity["upper"]).any():
        raise ValueError("Values larger than upper limit in column {}".format(colname))
    if "lower" in monotonocity and (series < monotonocity["lower"]).any():
        raise ValueError("Values larger than upper limit in column {}".format(colname))


def rows_to_be_fixed(series, column_monotonocity, digits, sign):
    """Compute boolean array of rows that must be modified

    Args:
        series (pd.Series):
        column_monotonocity (dict): Can contain "upper" or "lower"
            numerical bounds.
        digits (int): Accuracy required, how many digits
            that are to be printed, and to which we should relate
            constancy to.

    Returns:
        boolean series.
    """
    # minus epsilon is critical to avoid being greedy
    accuracy = 1.0 / 10.0 ** digits - epsilon
    if sign > 0:
        constants = series.round(digits + 1).diff() < accuracy
    else:
        constants = series.round(digits + 1).diff() > -accuracy

    # Allow constants at the lower and upper limits.
    if "upper" in column_monotonocity:
        constants = constants & (series < (column_monotonocity["upper"] - accuracy))
    if "lower" in column_monotonocity:
        constants = constants & (series > (column_monotonocity["lower"] + accuracy))
    return constants


def check_almost_monotone(series, digits, sign):
    """Raise a ValueError if a series is not sufficiently close
    to constant or monotone in a certain direction.

    Args:
        series (pd.Series): Vector of numbers
        digits (int):
        sign (int): direction
    """
    allowance = 1.0 / 10.0 ** (digits - 1)
    if sign > 0:
        if series.diff().min() < -allowance:
            raise ValueError("Series is not almost monotone")
    else:
        if series.diff().max() > allowance:
            raise ValueError("Series not not almost monotone")


def validate_monotonocity_arg(monotonocity, dframe_colnames):
    """
    Validate a dictionary with monotonocity arguments that
    can be given to df2str().

    Will raise ValueError exceptions if anything is wrong.

    Args:
        monotonocity (dict): Keys are 'sign', 'upper', 'lower'
            and  'allowzero'.
        dframe_colnames (list of str): Names of column names
            in dframes. Used in error messages.

    Returns:
        None
    """
    valid_keys = ["sign", "upper", "lower", "allowzero"]
    if monotonocity is None:
        return
    if not isinstance(monotonocity, dict):
        raise ValueError("monotonocity must be a dict")
    for col in monotonocity:
        if not isinstance(monotonocity[col], dict):
            raise ValueError("monotononicity must be a dict of dicts")
        if not set(monotonocity[col].keys()).issubset(valid_keys):
            raise ValueError(
                "Unknown keys in monotonocity {}".format(monotonocity[col].keys())
            )
        if col not in dframe_colnames:
            raise ValueError("Column %s does not exist in dataframe", str(col))
        if "sign" not in monotonocity[col]:
            raise ValueError("Monotonocity sign not specified for {}".format(col))
        try:
            signvalue = float(monotonocity[col]["sign"])
        except ValueError:
            raise ValueError(
                "Monotonocity sign {} not valid".format(monotonocity[col]["sign"])
            )
        if "upper" in monotonocity[col]:
            float(monotonocity[col]["upper"])
        if "lower" in monotonocity[col]:
            float(monotonocity[col]["lower"])
        if abs(signvalue) > 1:
            raise ValueError("Monotonocity sign must be -1 or +1, not larger/smaller")

        if "allowzero" in monotonocity[col]:
            if monotonocity[col]["allowzero"] not in {True, False}:
                raise ValueError(
                    "allowzero in monotonocity argument must be True/False"
                )


def remap_deprecated_monotonocity(monotone_column, monotone_direction):
    """Remove this function around pyscal 0.9"""
    signs = {"1": 1, "+1": 1, "inc": 1, "-1": -1, "dec": -1}
    if monotone_column is not None and monotone_direction is None:
        return {monotone_column: {"sign": -1}}
    if monotone_column is not None and monotone_direction is not None:
        if str(monotone_direction) not in signs:
            raise ValueError("Invalid monotone_direction {}".format(monotone_direction))
        return {monotone_column: {"sign": signs[str(monotone_direction)]}}
    return {}


def crosspoint(dframe, satcol, kr1col, kr2col):
    """Locate the crosspoint where kr1col == kr2col

    Args:
        dframe (pd.DataFrame): Dataframe with at least three columns
        satcol (str): Column name for the saturation column
        kr1col (str): Column name for first relperm column
        kr2col (str): Columnn ame for second column

    Returns:
        float, the saturation value (interpolated) where
            kr1col == kr2col, when krXcol is linearly interpolated
            as a function of the saturation values.
    """
    dframe = pd.DataFrame(dframe[[satcol, kr1col, kr2col]])  # Copy
    dframe.loc[:, "krdiff"] = dframe[kr1col] - dframe[kr2col]

    # Add a zero value for the difference column, and interpolate
    # the saturation column to the zero value
    zerodf = pd.DataFrame(index=[len(dframe)], data={"krdiff": 0.0})
    dframe = pd.concat([dframe, zerodf], sort=True).set_index("krdiff")

    if dframe.index.isnull().any():
        logger.warning("Could not compute crosspoint. Bug?")
        logger.debug(str(dframe))
        return -1

    dframe.interpolate(method="slinear", inplace=True)

    return dframe[np.isclose(dframe.index, 0.0)][satcol].values[0]


def estimate_diffjumppoint(table, xcol=None, ycol=None, side="right"):
    """Estimate the point where the y-data jumps from being linear
    in x to being nonlinear, or where it shift from one linear domain
    to another (for a piecewise linear function)

    If xcol is sw, and ycol is krw, and side is 'right', this
    will typically estimate sorw for you. If side is 'left' it will
    give you swcr.

    Args:
        table (pd.DataFrame): A Dataframe with x and y data
        xcol (string): The name of the column in table containing x-data. If
            None (default) the first column in table will be used.
        ycol (string): The name of the column in table containing y-data.
            If None (default) the second column in the table will be used.
        side (string): Must be 'left' or 'right'. Decides whether to look from
            the right side of the x-interval or from the left side for the
            linear domain.
    Returns:
        float: The x value where the start-linear domain ends.
    """

    if not xcol:
        xcol = table.columns[0]
    if not ycol:
        ycol = table.columns[1]
    assert isinstance(ycol, six.string_types)
    assert isinstance(xcol, six.string_types)
    if not side:
        raise ValueError("side cannot be None, use left or right")
    side = side.lower()
    assert side in ["left", "right"]

    # Compute the derivative:
    table["_deriv"] = table[ycol].diff() / table[xcol].diff()
    # The first becomes NaN, extrapolate from the second row:
    table.loc[0, "_deriv"] = table["_deriv"].iloc[1]

    # Pick the derivative at the first or last segment:
    iloc = {"left": 0, "right": -1}
    lin_a = table["_deriv"].iloc[iloc[side]]

    # Make a linear extrapolation from the last segment, starting at max x
    table["_linear"] = (table[xcol] - table[xcol].iloc[iloc[side]]) * lin_a + table[
        ycol
    ].iloc[iloc[side]]
    assert table["_linear"].values[iloc[side]] == table[ycol].values[iloc[side]]

    # Compute how much krw deviates from the linear krw:
    table["_lindev"] = (table[ycol] - table["_linear"]).abs()

    # Use the cumulative sum to determine the onset of non-zero deviation
    # starting from sw=1:
    table["_lindevcumsum"] = table["_lindev"].cumsum()

    if side == "right":
        maxcumsum = table["_lindevcumsum"].max()
        linearpart = table[(table["_lindevcumsum"] - maxcumsum).abs() < epsilon]
        return linearpart.iloc[1][xcol]
    # else:
    linearpart = table[(table["_lindevcumsum"] < epsilon)]
    if len(linearpart) == 1:
        linearpart = table[(table["_lindevcumsum"].shift(1) < epsilon)]
    return linearpart.iloc[-1][xcol]


def normalize_nonlinpart_wo(curve):
    """Make krw and krow functions that evaluate only on the
    (potentially) nonlinear part of the relperm curves, and with
    a normalized argument (0,1) on that interval.

    For a WaterOil krw curve, the nonlinear part is from swcr to sorw.
    swcr is mapped to zero, and 1 - sorw is mapped to 1. Then there is
    an assumed linear part from sorw to 1 which we ignore here.

    For a WaterOil krow curve, the nonlinear part
    is from 1 - sorw (mapped to zero) to swl (mapped to 1).

    These endpoints must be known the the WaterOil object coming in (the object
    can determine them using functions 'estimate_sorw()' and 'estimate_swcr()'

    If the entire curve is linear, it will not matter for this function, because
    this function only deals with the presumably known endpoints.

    Arguments:
        curve (WaterOil): incoming oilwater curve set (krw and krow)

    Returns:
        tuple of lambda functions. The first will evaluate krw on
            the normalized Sw interval [0,1], the second will
            evaluate krow on the normalized So interval [0,1].
    """
    krw_interp = interp1d(
        curve.table["sw"],
        curve.table["krw"],
        kind="linear",
        bounds_error=False,
        fill_value=(0.0, curve.table["krw"].max()),
    )

    # The internal dataframe might contain normalized
    # saturation values, but we do not want to assume they
    # are there or even correct, therefore we effectively
    # recalculate them
    def sw_fn(swn):
        return curve.swcr + swn * (1.0 - curve.swcr - curve.sorw)

    def krw_fn(swn):
        return krw_interp(sw_fn(swn))

    kro_interp = interp1d(
        1.0 - curve.table["sw"],
        curve.table["krow"],
        kind="linear",
        bounds_error=False,
        fill_value=(0.0, curve.table["krow"].max()),
    )

    def so_fn(son):
        return curve.sorw + son * (1.0 - curve.sorw - curve.swl)

    def kro_fn(son):
        return kro_interp(so_fn(son))

    return (krw_fn, kro_fn)


def normalize_nonlinpart_go(curve):
    """Make krg and krog functions that evaluates only on the
    (potentially) nonlinear part of the relperm curves, and with
    a normalized argument (0,1) on that interval.

    For a GasOil krg curve, the nonlinear part
    is from sgcr to sorg. sgcr is mapped to sg=zero, and sg=1 - sorg - swl is mapped
    to 1. Then there is an assumed linear part from sorg to 1 which we ignore here.

    For a GasOil krow curve, the nonlinear part
    is from 1 - sorg (mapped to zero) to sg=0 (mapped to 1).

    These endpoints must be known the the GasOil object coming in (the object
    can determine them using functions 'estimate_sorg()' and 'estimate_sgcr()'

    If the entire curve is linear, it will not matter for this function, because
    this function only deals with the presumably known endpoints.

    Arguments:
        curve (GasOil): incoming gasoil curve set (krg and krog)

    Returns:
        tuple of functions. The first will evaluate krg on
            the normalized Sg interval [0,1], the second will
            evaluate krog on the normalized So interval [0,1].
    """
    krg_interp = interp1d(
        curve.table["sg"],
        curve.table["krg"],
        kind="linear",
        bounds_error=False,
        fill_value=(0.0, curve.table["krg"].max()),
    )

    # The internal dataframe might contain normalized
    # saturation values, but we do not want to assume they
    # are there or even correct, therefore we effectively
    # recalculate them
    def sg_fn(sgn):
        return curve.sgcr + sgn * (1.0 - curve.swl - curve.sgcr - curve.sorg)

    def krg_fn(sgn):
        return krg_interp(sg_fn(sgn))

    kro_interp = interp1d(
        1.0 - curve.table["sg"],
        curve.table["krog"],
        kind="linear",
        bounds_error=False,
        fill_value=(0.0, curve.table["krog"].max()),
    )

    def so_fn(son):
        return curve.swl + curve.sorg + son * (1.0 - curve.swl - curve.sorg)

    def kro_fn(son):
        return kro_interp(so_fn(son))

    return (krg_fn, kro_fn)


def normalize_pc(curve):
    """Normalize the capillary pressure curve.

    This is only normalized with respect to the
    smallest and largest saturation present in the table,
    not to the could-be-uncertain swirr
    that the object could contain, because we then have
    to make assumptions on the equations used to generate
    the data in the table.

    Args:
        curve (WaterOil or GasOil): An object with a table with a pc column

    Returns:
        a lambda function that will evaluate pc on
        the normalized interval [0,1]
    """
    if isinstance(curve, pyscal.WaterOil):
        sat_col = "sw"
    elif isinstance(curve, pyscal.GasOil):
        sat_col = "sg"
    else:
        raise ValueError("Only WaterOil or GasOil allowed as argument")

    if "pc" not in curve.table:
        # Return a dummy zero lambda
        return lambda sxn: 0

    min_pc = curve.table["pc"].min()
    max_pc = curve.table["pc"].max()
    min_sx = curve.table[sat_col].min()
    max_sx = curve.table[sat_col].max()

    pc_interp = interp1d(
        curve.table[sat_col],
        curve.table["pc"],
        kind="linear",
        bounds_error=False,
        fill_value=(max_pc, min_pc),  # This gives constant extrapolation outside [0, 1]
    )

    # Map from normalized value to real saturation domain:
    def sx_fn(sxn):
        return curve.table[sat_col].min() + sxn * (max_sx - min_sx)

    def pc_fn(sxn):
        return pc_interp(sx_fn(sxn))

    return pc_fn


def _interpolate_tags(low, high, parameter, tag):
    """Preserve tag/comment. Depending on context, the
    interpolation parameter may or may not make sense. In a SCALrecommendation
    interpolation, the new tag should be constructed in the caller of this function.
    because of the way the parameter value is handled.

    This function is used by interpolate_wo and interpolate_go

    Args:
        low (WaterOil or GasOil): low case in interpolation
        high (WaterOil or GasOil): high case
        parameter (float): between 0 and 1
        tag (str): If not none, this is directly returned.

    Returns:
        string, a "computed" tag if a tag is not directly supplied
    """
    if tag is None:
        if low.tag == high.tag:
            if low.tag:
                return "Interpolated to {} in {}".format(parameter, low.tag)
            # No tags defined.
            return "Interpolated to {}".format(parameter)
        return "Interpolated to {} between {} and {}".format(
            parameter, low.tag, high.tag
        )
    return tag


def interpolate_wo(wo_low, wo_high, parameter, h=0.01, tag=None):
    """Interpolates between two water-oil curves.

    The saturation endpoints for the curves must be known
    by the objects. They can be estimated by estimate_sorw() etc.
    or can be set manually for finer control.

    The interpolation algorithm is different left and right
    for saturation endpoints, and saturation endpoints are
    interpolated individually.

    Arguments:
        wo_low (WaterOil): a "low" case
        wo_high (WaterOil): a "high" case
        parameter (float): Between 0 and 1. 0 will return the low case, 1 will return
            the high case. Any number in between will return an interpolated curve
        h (float): Saturation step-size in interpolant. If defaulted, a value
            smaller than in the input curves are used, to preserve information.
        tag (string): Tag to associate to the constructed object. If None
            it will be automatically filled. Set to empty string to ensure no tag.
    Returns:
        A new oil-water curve

    """
    # Warning: Almost code duplication with corresponding _go function

    assert isinstance(wo_low, pyscal.WaterOil)
    assert isinstance(wo_high, pyscal.WaterOil)

    assert 0 <= parameter <= 1
    # Extrapolation is refused, but perhaps later implemented with truncation to (0,1)

    # Constructs functions that works on normalized saturation interval
    krw1, kro1 = normalize_nonlinpart_wo(wo_low)
    krw2, kro2 = normalize_nonlinpart_wo(wo_high)
    pc1 = normalize_pc(wo_low)
    pc2 = normalize_pc(wo_high)

    # Construct a function that can be applied to both relperm values
    # and endpoints
    def weighted_value(a, b):
        return a * (1.0 - parameter) + b * parameter

    # Interpolate saturation endpoints
    swl_new = weighted_value(wo_low.swl, wo_high.swl)
    swcr_new = weighted_value(wo_low.swcr, wo_high.swcr)
    sorw_new = weighted_value(wo_low.sorw, wo_high.sorw)

    # Interpolate kr at saturation endpoints
    krwmax_new = weighted_value(wo_low.table["krw"].max(), wo_high.table["krw"].max())
    krwend_new = weighted_value(krw1(1), krw2(1))
    kroend_new = weighted_value(kro1(1), kro2(1))

    # Construct the new WaterOil object, with interpolated
    # endpoints:
    wo_new = pyscal.WaterOil(swl=swl_new, swcr=swcr_new, sorw=sorw_new, h=h)

    # Add interpolated relperm data in nonlinear parts:
    wo_new.table["krw"] = weighted_value(
        krw1(wo_new.table["swn"]), krw2(wo_new.table["swn"])
    )
    wo_new.table["krow"] = weighted_value(
        kro1(wo_new.table["son"]), kro2(wo_new.table["son"])
    )

    wo_new.set_endpoints_linearpart_krw(krwend=krwend_new, krwmax=krwmax_new)
    wo_new.set_endpoints_linearpart_krow(kroend=kroend_new)

    # We need a new fit-for-purpose normalized swnpc, that ignores
    # the initial swnpc (swirr-influenced)
    wo_new.table["swn_pc_intp"] = (wo_new.table["sw"] - wo_new.table["sw"].min()) / (
        wo_new.table["sw"].max() - wo_new.table["sw"].min()
    )
    wo_new.table["pc"] = weighted_value(
        pc1(wo_new.table["swn_pc_intp"]), pc2(wo_new.table["swn_pc_intp"])
    )

    wo_new.tag = _interpolate_tags(wo_low, wo_high, parameter, tag)

    return wo_new


def comment_formatter(multiline, prefix="-- "):
    """Prepends comment characters to every line in input

    Args:
        multiline (str): String that can contain newlines
        prefix (str): Comment characters to prepend every line with
            Default is the Eclipse comment syntax '-- '

    Returns:
        string, with newlines preserved, and where each line
            starts with the given prefix. Always ends with a newline.
    """
    if multiline is None or not multiline.strip():
        # Ensure we indicate that there is placeholder for something.
        return "-- \n"
    return (
        "\n".join([prefix + line.strip() for line in multiline.splitlines()]).strip()
        + "\n"
    )


def interpolate_go(go_low, go_high, parameter, h=0.01, tag=None):
    """Interpolates between two gas-oil curves.

    The saturation endpoints for the curves must be known
    by the objects. They can be estimated by estimate_sorg() etc.
    or can be set manually for finer control.

    The interpolation algorithm is different left and right
    for saturation endpoints, and saturation endpoints are
    interpolated individually.

    Arguments:
        go_low (GasOil): a "low" case
        go_high (GasOil): a "high" case
        parameter (float): Between 0 and 1. 0 will return the low case, 1 will return
            the high case. Any number in between will return an interpolated curve
        h (float): Saturation step-size in interpolant. If defaulted, a value
            smaller than in the input curves are used, to preserve information.
        tag (string): Tag to associate to the constructed object. If None
            it will be automatically filled. Set to empty string to ensure no tag.
    Returns:
        A new gas-oil curve

    """
    # Warning: Almost code duplication with corresponding _wo function

    assert isinstance(go_low, pyscal.GasOil)
    assert isinstance(go_high, pyscal.GasOil)

    assert 0 <= parameter <= 1
    # Extrapolation is refused, but perhaps later implemented with truncation to (0,1)

    # Constructs functions that works on normalized saturation interval
    krg1, kro1 = normalize_nonlinpart_go(go_low)
    krg2, kro2 = normalize_nonlinpart_go(go_high)
    pc1 = normalize_pc(go_low)
    pc2 = normalize_pc(go_high)

    # Construct a lambda function that can be applied to both relperm values
    # and endpoints
    def weighted_value(a, b):
        return a * (1.0 - parameter) + b * parameter

    # Interpolate saturation endpoints
    swl_new = weighted_value(go_low.swl, go_high.swl)
    sgcr_new = weighted_value(go_low.sgcr, go_high.sgcr)
    sorg_new = weighted_value(go_low.sorg, go_high.sorg)

    # Interpolate kr at saturation endpoints
    krgmax_new = weighted_value(go_low.table["krg"].max(), go_high.table["krg"].max())
    krgend_new = weighted_value(krg1(1), krg2(1))
    kroend_new = weighted_value(kro1(1), kro2(1))

    # Construct the new GasOil object, with interpolated
    # endpoints:
    go_new = pyscal.GasOil(swl=swl_new, sgcr=sgcr_new, sorg=sorg_new, h=h)

    # Add interpolated relperm data in nonlinear parts:
    go_new.table["krg"] = weighted_value(
        krg1(go_new.table["sgn"]), krg2(go_new.table["sgn"])
    )
    go_new.table["krog"] = weighted_value(
        kro1(go_new.table["son"]), kro2(go_new.table["son"])
    )
    go_new.table["pc"] = weighted_value(
        pc1(go_new.table["sgn"]), pc2(go_new.table["sgn"])
    )

    # We need a new fit-for-purpose normalized sgnpc
    go_new.table["sgn_pc_intp"] = (go_new.table["sg"] - go_new.table["sg"].min()) / (
        go_new.table["sg"].max() - go_new.table["sg"].min()
    )
    go_new.table["pc"] = weighted_value(
        pc1(go_new.table["sgn_pc_intp"]), pc2(go_new.table["sgn_pc_intp"])
    )

    go_new.set_endpoints_linearpart_krog(kroend=kroend_new)

    # Here we should have honored krgendanchor. Check github issue.
    go_new.set_endpoints_linearpart_krg(krgend=krgend_new, krgmax=krgmax_new)

    go_new.tag = _interpolate_tags(go_low, go_high, parameter, tag)

    return go_new


def interpolator(
    tableobject, wo_low, wo_high, parameter, sat="sw", kr1="krw", kr2="krow", pc="pc"
):
    """Interpolates between two curves.

    DEPRECATED FUNCTION!

    The interpolation parameter is 0 through 1,
    irrespective of phases or low-base/base-high/low-high.

    Args:
        tabjeobject (WaterOil or GasOil): A partially setup object where
            relperm and pc columns are to be filled with numbers.
        wo_low (WaterOil or GasOil): "Low" case of interpolation (relates
            to interpolation parameter 0). Must be copies, as they
            will be modified.
        wo_high: Ditto, relates to interpolation parameter 1
        parameter (float): Between 0 and 1, what you want to interpolate to.
        sat (str): Name of the saturation column, typically 'sw' or 'sg'
        kr1 (str): Name of the first relperm column ('krw' or 'krg')
        kr2 (str): Name of the second relperm column ('krow' or 'krog')
        pc (str): Name of the capillary pressure column ('pc')

    Returns:
        None, but modifies the first argument.
    """
    logger.warning("utils.interpolator() is deprecated and will disappear")

    wo_low.table.rename(columns={kr1: kr1 + "_1"}, inplace=True)
    wo_high.table.rename(columns={kr1: kr1 + "_2"}, inplace=True)
    wo_low.table.rename(columns={kr2: kr2 + "_1"}, inplace=True)
    wo_high.table.rename(columns={kr2: kr2 + "_2"}, inplace=True)
    wo_low.table.rename(columns={pc: pc + "_1"}, inplace=True)
    wo_high.table.rename(columns={pc: pc + "_2"}, inplace=True)

    # Result data container:
    satresult = pd.DataFrame(data=tableobject.table[sat], columns=[sat])

    # Merge swresult with wo_low and wo_high, and interpolate all
    # columns in sw:
    intdf = (
        pd.concat([wo_low.table, wo_high.table, satresult], sort=True)
        .set_index(sat)
        .sort_index()
        .interpolate(method="slinear")
        .fillna(method="bfill")
        .fillna(method="ffill")
    )

    # Normalized saturations does not make sense for the
    # interpolant, remove:
    for col in ["swn", "son", "swnpc", "H", "J"]:
        if col in intdf.columns:
            del intdf[col]

    intdf[kr1] = intdf[kr1 + "_1"] * (1 - parameter) + intdf[kr1 + "_2"] * parameter
    intdf[kr2] = intdf[kr2 + "_1"] * (1 - parameter) + intdf[kr2 + "_2"] * parameter
    if pc + "_1" in wo_low.table.columns and pc + "_2" in wo_high.table.columns:
        intdf[pc] = intdf[pc + "_1"] * (1 - parameter) + intdf[pc + "_2"] * parameter
    else:
        intdf[pc] = 0

    # Slice out the resulting sw values and columns. Slicing on
    # floating point indices is not robust so we need to slice on an
    # integer version of the sw column
    tableobject.table["swint"] = list(
        map(int, list(map(round, tableobject.table[sat] * SWINTEGERS)))
    )
    intdf["swint"] = list(map(int, list(map(round, intdf.index.values * SWINTEGERS))))
    intdf = intdf.reset_index()
    intdf.drop_duplicates("swint", inplace=True)
    intdf.set_index("swint", inplace=True)
    intdf = intdf.loc[tableobject.table["swint"].values]
    intdf = intdf[[sat, kr1, kr2, pc]].reset_index()

    # intdf['swint'] = (intdf['sw'] * SWINTEGERS).astype(int)
    # intdf.drop_duplicates('swint', inplace=True)

    # Populate the WaterOil object
    tableobject.table[kr1] = intdf[kr1]
    tableobject.table[kr2] = intdf[kr2]
    tableobject.table[pc] = intdf[pc]
    tableobject.table.fillna(method="ffill", inplace=True)
