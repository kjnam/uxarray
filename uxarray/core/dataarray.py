from __future__ import annotations

import xarray as xr
import numpy as np


from typing import TYPE_CHECKING, Optional, Hashable, Literal

from uxarray.formatting_html import array_repr

from html import escape

from xarray.core.options import OPTIONS

from uxarray.grid import Grid
import uxarray.core.dataset

if TYPE_CHECKING:
    from uxarray.core.dataset import UxDataset

from xarray.core.utils import UncachedAccessor


from uxarray.core.gradient import (
    _calculate_grad_on_edge_from_faces,
    _calculate_edge_face_difference,
    _calculate_edge_node_difference,
)

from uxarray.plot.accessor import UxDataArrayPlotAccessor
from uxarray.subset import DataArraySubsetAccessor
from uxarray.remap import UxDataArrayRemapAccessor
from uxarray.core.aggregation import _uxda_grid_aggregate

import warnings

import cartopy.crs as ccrs


class UxDataArray(xr.DataArray):
    """N-dimensional ``xarray.DataArray``-like array. Inherits from
    ``xarray.DataArray`` and has its own unstructured grid-aware array
    operators and attributes through the ``uxgrid`` accessor.

    Parameters
    ----------
    uxgrid : uxarray.Grid, optional
        The `Grid` object that makes this array aware of the unstructured
        grid topology it belongs to.
        If `None`, it needs to be an instance of `uxarray.Grid`.

    Other Parameters
    ----------------
    *args:
        Arguments for the ``xarray.DataArray`` class
    **kwargs:
        Keyword arguments for the ``xarray.DataArray`` class

    Notes
    -----
    See `xarray.DataArray <https://docs.xarray.dev/en/stable/generated/xarray.DataArray.html>`__
    for further information about DataArrays.
    """

    # expected instance attributes, required for subclassing with xarray (as of v0.13.0)
    __slots__ = ("_uxgrid",)

    def __init__(self, *args, uxgrid: Grid = None, **kwargs):
        self._uxgrid = None

        if uxgrid is not None and not isinstance(uxgrid, Grid):
            raise RuntimeError(
                "uxarray.UxDataArray.__init__: uxgrid can be either None or "
                "an instance of the uxarray.Grid class"
            )
        else:
            self.uxgrid = uxgrid

        super().__init__(*args, **kwargs)

    # declare various accessors
    plot = UncachedAccessor(UxDataArrayPlotAccessor)
    subset = UncachedAccessor(DataArraySubsetAccessor)
    remap = UncachedAccessor(UxDataArrayRemapAccessor)

    def _repr_html_(self) -> str:
        if OPTIONS["display_style"] == "text":
            return f"<pre>{escape(repr(self))}</pre>"
        return array_repr(self)

    @classmethod
    def _construct_direct(cls, *args, **kwargs):
        """Override to make the result a ``uxarray.UxDataArray`` class."""
        return cls(xr.DataArray._construct_direct(*args, **kwargs))

    def _copy(self, **kwargs):
        """Override to make the result a complete instance of
        ``uxarray.UxDataArray``."""
        copied = super()._copy(**kwargs)

        deep = kwargs.get("deep", None)

        if deep:
            # Reinitialize the uxgrid assessor
            copied.uxgrid = self.uxgrid.copy()  # deep copy
        else:
            # Point to the existing uxgrid object
            copied.uxgrid = self.uxgrid

        return copied

    def _replace(self, *args, **kwargs):
        """Override to make the result a complete instance of
        ``uxarray.UxDataArray``."""
        da = super()._replace(*args, **kwargs)

        if isinstance(da, UxDataArray):
            da.uxgrid = self.uxgrid
        else:
            da = UxDataArray(da, uxgrid=self.uxgrid)

        return da

    @property
    def uxgrid(self):
        """``uxarray.Grid`` property for ``uxarray.UxDataArray`` to make it
        unstructured grid-aware.

        Examples
        --------
        uxds = ux.open_dataset(grid_path, data_path)
        uxds.<variable_name>.uxgrid
        """
        return self._uxgrid

    # a setter function
    @uxgrid.setter
    def uxgrid(self, ugrid_obj):
        self._uxgrid = ugrid_obj

    def to_geodataframe(self, override=False, cache=True, exclude_antimeridian=False):
        """Constructs a ``spatialpandas.GeoDataFrame`` with a "geometry"
        column, containing a collection of Shapely Polygons or MultiPolygons
        representing the geometry of the unstructured grid, and a data column
        representing a 1D slice of data mapped to each Polygon.

        Parameters
        override: bool
            Flag to recompute the ``GeoDataFrame`` stored under the ``uxgrid`` if one is already cached
        cache: bool
            Flag to indicate if the computed ``GeoDataFrame`` stored under the ``uxgrid`` accessor should be cached
        exclude_antimeridian: bool, Optional
            Selects whether to exclude any face that contains an edge that crosses the antimeridian

        Returns
        -------
        gdf : spatialpandas.GeoDataFrame
            The output `GeoDataFrame` with a filled out "geometry" and 1D data column representing the geometry of the unstructured grid
        """

        if self.values.ndim > 1:
            # data is multidimensional, must be a 1D slice
            raise ValueError(
                f"Data Variable must be 1-dimensional, with shape {self.uxgrid.n_face} "
                f"for face-centered data."
            )

        if self.values.size == self.uxgrid.n_face:
            gdf = self.uxgrid.to_geodataframe(
                override=override,
                cache=cache,
                exclude_antimeridian=exclude_antimeridian,
            )

            var_name = self.name if self.name is not None else "var"

            if exclude_antimeridian:
                gdf[var_name] = np.delete(
                    self.values, self.uxgrid.antimeridian_face_indices, axis=0
                )
            else:
                gdf[var_name] = self.values
            return gdf

        elif self.values.size == self.uxgrid.n_node:
            raise ValueError(
                f"Data Variable with size {self.values.size} does not match the number of faces "
                f"({self.uxgrid.n_face}. Current size matches the number of nodes."
            )

        # data not mapped to faces or nodes
        else:
            raise ValueError(
                f"Data Variable with size {self.values.size} does not match the number of faces "
                f"({self.uxgrid.n_face}."
            )

    def to_polycollection(
        self,
        periodic_elements: Optional[str] = "exclude",
        projection: Optional[ccrs.Projection] = None,
        return_indices: Optional[bool] = False,
        cache: Optional[bool] = True,
        override: Optional[bool] = False,
    ):
        """Converts a ``UxDataArray`` to a
        ``matplotlib.collections.PolyCollection``, representing each face as a
        polygon shaded with a face-centered data variable.

        Parameters
        ----------
        periodic_elements: str
            Method for handling elements that cross the antimeridian. One of ['include', 'exclude', 'split']
        projection: ccrs.Projection
            Cartopy geographic projection to use
        return_indices: bool
            Flag to indicate whether to return the indices of corrected polygons, if any exist
        cache: bool
            Flag to indicate whether to cache the computed PolyCollection
        override: bool
            Flag to indicate whether to override a cached PolyCollection, if it exists
        """
        # data is multidimensional, must be a 1D slice
        if self.values.ndim > 1:
            raise ValueError(
                f"Data Variable must be 1-dimensional, with shape {self.uxgrid.n_face} "
                f"for face-centered data."
            )

        if self._face_centered():
            poly_collection, corrected_to_original_faces = (
                self.uxgrid.to_polycollection(
                    override=override,
                    cache=cache,
                    periodic_elements=periodic_elements,
                    return_indices=True,
                    projection=projection,
                )
            )

            # map data with antimeridian polygons
            if len(corrected_to_original_faces) > 0:
                data = self.values[corrected_to_original_faces]
            else:
                data = self.values

            poly_collection.set_array(data)

            if return_indices:
                return poly_collection, corrected_to_original_faces
            else:
                return poly_collection
        else:
            raise ValueError("Data variable must be face centered.")

    def to_dataset(
        self,
        dim: Hashable = None,
        *,
        name: Hashable = None,
        promote_attrs: bool = False,
    ) -> UxDataset:
        """Convert a UxDataArray to a UxDataset.

        Parameters
        ----------
        dim : Hashable, optional
            Name of the dimension on this array along which to split this array
            into separate variables. If not provided, this array is converted
            into a Dataset of one variable.
        name : Hashable, optional
            Name to substitute for this array's name. Only valid if ``dim`` is
            not provided.
        promote_attrs : bool, default: False
            Set to True to shallow copy attrs of UxDataArray to returned UxDataset.

        Returns
        -------
        uxds: UxDataSet
        """
        xrds = super().to_dataset(dim=dim, name=name, promote_attrs=promote_attrs)
        uxds = uxarray.core.dataset.UxDataset(xrds, uxgrid=self.uxgrid)

        return uxds

    def integrate(
        self, quadrature_rule: Optional[str] = "triangular", order: Optional[int] = 4
    ) -> UxDataArray:
        """Computes the integral of a data variable residing on an unstructured
        grid.

        Parameters
        ----------
        quadrature_rule : str, optional
            Quadrature rule to use. Defaults to "triangular".
        order : int, optional
            Order of quadrature rule. Defaults to 4.

        Returns
        -------
        uxda : UxDataArray
            UxDataArray containing the integrated data variable

        Examples
        --------
        >>> import uxarray as ux
        >>> uxds = ux.open_dataset("grid.ug", "centroid_pressure_data_ug")

        # Compute the integral
        >>> integral = uxds['psi'].integrate()
        """
        if self.values.shape[-1] == self.uxgrid.n_face:
            face_areas, face_jacobian = self.uxgrid.compute_face_areas(
                quadrature_rule, order
            )

            # perform dot product between face areas and last dimension of data
            integral = np.einsum("i,...i", face_areas, self.values)

        elif self.values.shape[-1] == self.uxgrid.n_node:
            raise ValueError("Integrating data mapped to each node not yet supported.")

        elif self.values.shape[-1] == self.uxgrid.n_edge:
            raise ValueError("Integrating data mapped to each edge not yet supported.")

        else:
            raise ValueError(
                f"The final dimension of the data variable does not match the number of nodes, edges, "
                f"or faces. Expected one of "
                f"{self.uxgrid.n_node}, {self.uxgrid.n_edge}, or {self.uxgrid.n_face}, "
                f"but received {self.values.shape[-1]}"
            )

        # construct a uxda with integrated quantity
        uxda = UxDataArray(
            integral, uxgrid=self.uxgrid, dims=self.dims[:-1], name=self.name
        )

        return uxda

    def nodal_average(self):
        """Computes the Nodal Average of a Data Variable, which is the mean of
        the nodes that surround each face.

        Can be used for remapping node-centered data to each face.
        """

        warnings.warn(
            "This function will be deprecated in a future release. Please use uxda.mean(destination=`face`) instead.",
            DeprecationWarning,
        )

        return self.topological_mean(destination="face")

    def topological_mean(
        self,
        destination: Literal["node", "edge", "face"],
        **kwargs,
    ):
        """Performs a topological mean aggregation.

        See Also
        --------
        numpy.mean
        dask.array.mean
        xarray.DataArray.mean

        Parameters
        ----------
        destination: str,
            Destination grid dimension for aggregation.

            Node-Centered Variable:
            - ``destination='edge'``: Aggregation is applied on the nodes that saddle each edge, with the result stored
            on each edge
            - ``destination='face'``: Aggregation is applied on the nodes that surround each face, with the result stored
            on each face.

            Edge-Centered Variable:
            - ``destination='node'``: Aggregation is applied on the edges that intersect each node, with the result stored
            on each node.
            - ``Destination='face'``: Aggregation is applied on the edges that surround each face, with the result stored
            on each face.

            Face-Centered Variable:
            - ``destination='node'``: Aggregation is applied on the faces that saddle each node, with the result stored
            on each node.
            - ``Destination='edge'``: Aggregation is applied on the faces that saddle each edge, with the result stored
            on each edge.


        Returns
        -------
        reduced: UxDataArray
            New UxDataArray with ``mean`` applied to its data.
        """
        return _uxda_grid_aggregate(self, destination, "mean", **kwargs)

    def topological_min(
        self,
        destination=None,
        **kwargs,
    ):
        """Performs a topological min aggregation.

        See Also
        --------
        numpy.min
        dask.array.min
        xarray.DataArray.min

        Parameters
        ----------
        destination: str,
            Destination grid dimension for Aggregation.

            Node-Centered Variable:
            - ``destination='edge'``: Aggregation is applied on the nodes that saddle each edge, with the result stored
            on each edge
            - ``destination='face'``: Aggregation is applied on the nodes that surround each face, with the result stored
            on each face.

            Edge-Centered Variable:
            - ``destination='node'``: Aggregation is applied on the edges that intersect each node, with the result stored
            on each node.
            - ``Destination='face'``: Aggregation is applied on the edges that surround each face, with the result stored
            on each face.

            Face-Centered Variable:
            - ``destination='node'``: Aggregation is applied on the faces that saddle each node, with the result stored
            on each node.
            - ``Destination='edge'``: Aggregation is applied on the faces that saddle each edge, with the result stored
            on each edge.


        Returns
        -------
        reduced: UxDataArray
            New UxDataArray with ``min`` applied to its data.
        """
        return _uxda_grid_aggregate(self, destination, "min", **kwargs)

    def topological_max(
        self,
        destination=None,
        **kwargs,
    ):
        """Performs a topological max aggregation.

        See Also
        --------
        numpy.max
        dask.array.max
        xarray.DataArray.max

        Parameters
        ----------
        destination: str,
            Destination grid dimension for Aggregation.

            Node-Centered Variable:
            - ``destination='edge'``: Aggregation is applied on the nodes that saddle each edge, with the result stored
            on each edge
            - ``destination='face'``: Aggregation is applied on the nodes that surround each face, with the result stored
            on each face.

            Edge-Centered Variable:
            - ``destination='node'``: Aggregation is applied on the edges that intersect each node, with the result stored
            on each node.
            - ``Destination='face'``: Aggregation is applied on the edges that surround each face, with the result stored
            on each face.

            Face-Centered Variable:
            - ``destination='node'``: Aggregation is applied on the faces that saddle each node, with the result stored
            on each node.
            - ``Destination='edge'``: Aggregation is applied on the faces that saddle each edge, with the result stored
            on each edge.


        Returns
        -------
        reduced: UxDataArray
            New UxDataArray with ``max`` applied to its data.
        """

        return _uxda_grid_aggregate(self, destination, "max", **kwargs)

    def topological_median(
        self,
        destination=None,
        **kwargs,
    ):
        """Performs a topological median aggregation.

        See Also
        --------
        numpy.median
        dask.array.median
        xarray.DataArray.median

        Parameters
        ----------

        destination: str,
            Destination grid dimension for Aggregation.

            Node-Centered Variable:
            - ``destination='edge'``: Aggregation is applied on the nodes that saddle each edge, with the result stored
            on each edge
            - ``destination='face'``: Aggregation is applied on the nodes that surround each face, with the result stored
            on each face.

            Edge-Centered Variable:
            - ``destination='node'``: Aggregation is applied on the edges that intersect each node, with the result stored
            on each node.
            - ``Destination='face'``: Aggregation is applied on the edges that surround each face, with the result stored
            on each face.

            Face-Centered Variable:
            - ``destination='node'``: Aggregation is applied on the faces that saddle each node, with the result stored
            on each node.
            - ``Destination='edge'``: Aggregation is applied on the faces that saddle each edge, with the result stored
            on each edge.


        Returns
        -------
        reduced: UxDataArray
            New UxDataArray with ``median`` applied to its data.
        """
        return _uxda_grid_aggregate(self, destination, "median", **kwargs)

    def topological_std(
        self,
        destination=None,
        **kwargs,
    ):
        """Performs a topological std aggregation.

        See Also
        --------
        numpy.std
        dask.array.std
        xarray.DataArray.std

        Parameters
        ----------
        destination: str,
            Destination grid dimension for Aggregation.

            Node-Centered Variable:
            - ``destination='edge'``: Aggregation is applied on the nodes that saddle each edge, with the result stored
            on each edge
            - ``destination='face'``: Aggregation is applied on the nodes that surround each face, with the result stored
            on each face.

            Edge-Centered Variable:
            - ``destination='node'``: Aggregation is applied on the edges that intersect each node, with the result stored
            on each node.
            - ``Destination='face'``: Aggregation is applied on the edges that surround each face, with the result stored
            on each face.

            Face-Centered Variable:
            - ``destination='node'``: Aggregation is applied on the faces that saddle each node, with the result stored
            on each node.
            - ``Destination='edge'``: Aggregation is applied on the faces that saddle each edge, with the result stored
            on each edge.


        Returns
        -------
        reduced: UxDataArray
            New UxDataArray with ``std`` applied to its data.
        """
        return _uxda_grid_aggregate(self, destination, "std", **kwargs)

    def topological_var(
        self,
        destination=None,
        **kwargs,
    ):
        """Performs a topological var aggregation.

        See Also
        --------
        numpy.var
        dask.array.var
        xarray.DataArray.var

        Parameters
        ----------

        destination: str,
            Destination grid dimension for Aggregation.

            Node-Centered Variable:
            - ``destination='edge'``: Aggregation is applied on the nodes that saddle each edge, with the result stored
            on each edge
            - ``destination='face'``: Aggregation is applied on the nodes that surround each face, with the result stored
            on each face.

            Edge-Centered Variable:
            - ``destination='node'``: Aggregation is applied on the edges that intersect each node, with the result stored
            on each node.
            - ``Destination='face'``: Aggregation is applied on the edges that surround each face, with the result stored
            on each face.

            Face-Centered Variable:
            - ``destination='node'``: Aggregation is applied on the faces that saddle each node, with the result stored
            on each node.
            - ``Destination='edge'``: Aggregation is applied on the faces that saddle each edge, with the result stored
            on each edge.


        Returns
        -------
        reduced: UxDataArray
            New UxDataArray with ``var`` applied to its data.
        """
        return _uxda_grid_aggregate(self, destination, "var", **kwargs)

    def topological_sum(
        self,
        destination=None,
        **kwargs,
    ):
        """Performs a topological sum aggregation.

        See Also
        --------
        numpy.sum
        dask.array.sum
        xarray.DataArray.sum

        Parameters
        ----------
        destination: str,
            Destination grid dimension for Aggregation.

            Node-Centered Variable:
            - ``destination='edge'``: Aggregation is applied on the nodes that saddle each edge, with the result stored
            on each edge
            - ``destination='face'``: Aggregation is applied on the nodes that surround each face, with the result stored
            on each face.

            Edge-Centered Variable:
            - ``destination='node'``: Aggregation is applied on the edges that intersect each node, with the result stored
            on each node.
            - ``Destination='face'``: Aggregation is applied on the edges that surround each face, with the result stored
            on each face.

            Face-Centered Variable:
            - ``destination='node'``: Aggregation is applied on the faces that saddle each node, with the result stored
            on each node.
            - ``Destination='edge'``: Aggregation is applied on the faces that saddle each edge, with the result stored
            on each edge.


        Returns
        -------
        reduced: UxDataArray
            New UxDataArray with ``sum`` applied to its data.
        """
        return _uxda_grid_aggregate(self, destination, "sum", **kwargs)

    def topological_prod(
        self,
        destination=None,
        **kwargs,
    ):
        """Performs a topological prod aggregation.

        See Also
        --------
        numpy.prod
        dask.array.prod
        xarray.DataArray.prod

        Parameters

        destination: str,
            Destination grid dimension for Aggregation.

            Node-Centered Variable:
            - ``destination='edge'``: Aggregation is applied on the nodes that saddle each edge, with the result stored
            on each edge
            - ``destination='face'``: Aggregation is applied on the nodes that surround each face, with the result stored
            on each face.

            Edge-Centered Variable:
            - ``destination='node'``: Aggregation is applied on the edges that intersect each node, with the result stored
            on each node.
            - ``Destination='face'``: Aggregation is applied on the edges that surround each face, with the result stored
            on each face.

            Face-Centered Variable:
            - ``destination='node'``: Aggregation is applied on the faces that saddle each node, with the result stored
            on each node.
            - ``Destination='edge'``: Aggregation is applied on the faces that saddle each edge, with the result stored
            on each edge.


        Returns
        -------
        reduced: UxDataArray
            New UxDataArray with ``prod`` applied to its data.
        """
        return _uxda_grid_aggregate(self, destination, "prod", **kwargs)

    def topological_all(
        self,
        destination=None,
        **kwargs,
    ):
        """Performs a topological all aggregation.

        See Also
        --------
        numpy.all
        dask.array.all
        xarray.DataArray.all

        Parameters
        ----------
        destination: str,
            Destination grid dimension for Aggregation.

            Node-Centered Variable:
            - ``destination='edge'``: Aggregation is applied on the nodes that saddle each edge, with the result stored
            on each edge
            - ``destination='face'``: Aggregation is applied on the nodes that surround each face, with the result stored
            on each face.

            Edge-Centered Variable:
            - ``destination='node'``: Aggregation is applied on the edges that intersect each node, with the result stored
            on each node.
            - ``Destination='face'``: Aggregation is applied on the edges that surround each face, with the result stored
            on each face.

            Face-Centered Variable:
            - ``destination='node'``: Aggregation is applied on the faces that saddle each node, with the result stored
            on each node.
            - ``Destination='edge'``: Aggregation is applied on the faces that saddle each edge, with the result stored
            on each edge.


        Returns
        -------
        reduced: UxDataArray
            New UxDataArray with ``all`` applied to its data.
        """
        return _uxda_grid_aggregate(self, destination, "all", **kwargs)

    def topological_any(
        self,
        destination=None,
        **kwargs,
    ):
        """Performs a topological any aggregation.

        See Also
        --------
        numpy.any
        dask.array.any
        xarray.DataArray.any

        Parameters
        ----------
        destination: str,
            Destination grid dimension for Aggregation.

            Node-Centered Variable:
            - ``destination='edge'``: Aggregation is applied on the nodes that saddle each edge, with the result stored
            on each edge
            - ``destination='face'``: Aggregation is applied on the nodes that surround each face, with the result stored
            on each face.

            Edge-Centered Variable:
            - ``destination='node'``: Aggregation is applied on the edges that intersect each node, with the result stored
            on each node.
            - ``Destination='face'``: Aggregation is applied on the edges that surround each face, with the result stored
            on each face.

            Face-Centered Variable:
            - ``destination='node'``: Aggregation is applied on the faces that saddle each node, with the result stored
            on each node.
            - ``Destination='edge'``: Aggregation is applied on the faces that saddle each edge, with the result stored
            on each edge.


        Returns
        -------
        reduced: UxDataArray
            New UxDataArray with ``any`` applied to its data.
        """
        return _uxda_grid_aggregate(self, destination, "any", **kwargs)

    def gradient(
        self, normalize: Optional[bool] = False, use_magnitude: Optional[bool] = True
    ):
        """Computes the horizontal gradient of a data variable residing on an
        unstructured grid.

        Currently only supports gradients of face-centered data variables, with the resulting gradient being stored
        on each edge. The gradient of a node-centered data variable can be approximated by computing the nodal average
        and then computing the gradient.

        The aboslute value of the gradient is used, since UXarray does not yet support representing the direction
        of the gradient.

        The expression for calculating the gradient on each edge comes from Eq. 22 in Ringler et al. (2010), J. Comput. Phys.

        Code is adapted from https://github.com/theweathermanda/MPAS_utilities/blob/main/mpas_calc_operators.py


        Parameters
        ----------
        use_magnitude : bool, default=True
            Whether to use the magnitude (aboslute value) of the resulting gradient
        normalize: bool, default=None
            Whether to normalize (l2) the resulting gradient

        Example
        -------
        Face-centered variable
        >>> uxds['var'].gradient()
        Node-centered variable
        >>> uxds['var'].nodal_average().gradient()
        """

        if not self._face_centered():
            raise ValueError(
                "Gradient computations are currently only supported for face-centered data variables. For node-centered"
                "data, consider performing a nodal average or remapping to faces."
            )

        if use_magnitude is False:
            warnings.warn(
                "Gradients can only be represented in terms of their aboslute value, since UXarray does not "
                "currently store any information for representing the sign."
            )

        _grad = _calculate_grad_on_edge_from_faces(
            d_var=self.values,
            edge_faces=self.uxgrid.edge_face_connectivity.values,
            edge_face_distances=self.uxgrid.edge_face_distances.values,
            n_edge=self.uxgrid.n_edge,
            normalize=normalize,
        )

        dims = list(self.dims)
        dims[-1] = "n_edge"

        uxda = UxDataArray(
            _grad,
            uxgrid=self.uxgrid,
            dims=dims,
            name=self.name + "_grad" if self.name is not None else "grad",
        )

        return uxda

    def difference(self, destination: Optional[str] = "edge"):
        """Computes the absolute difference between a data variable.

        The difference for a face-centered data variable can be computed on each edge using the ``edge_face_connectivity``,
        specified by ``destination='edge'``.

        The difference for a node-centered data variable can be computed on each edge using the ``edge_node_connectivity``,
        specified by ``destination='edge'``.

        Computing the difference for an edge-centered data variable is not yet supported.

        Note
        ----
        Not to be confused with the ``.diff()`` method from xarray.
        https://docs.xarray.dev/en/stable/generated/xarray.DataArray.diff.html

        Parameters
        ----------
        destination: {‘node’, ‘edge’, ‘face’}, default='edge''
            The desired destination for computing the difference across and storing on
        """

        if destination not in ["node", "edge", "face"]:
            raise ValueError(
                f"Invalid destination '{destination}'. Must be one of ['node', 'edge', 'face']"
            )

        dims = list(self.dims)
        var_name = str(self.name) + "_" if self.name is not None else " "

        if self._face_centered():
            if destination == "edge":
                _difference = _calculate_edge_face_difference(
                    self.values,
                    self.uxgrid.edge_face_connectivity.values,
                    self.uxgrid.n_edge,
                )
                dims[-1] = "n_edge"
                name = f"{var_name}edge_face_difference"
            elif destination == "face":
                raise ValueError(
                    "Invalid destination 'face' for a face-centered data variable, computing"
                    "the difference and storing it on each face is not possible"
                )
            elif destination == "node":
                raise ValueError(
                    "Support for computing the difference of a face-centered data variable and storing"
                    "the result on each node not yet supported."
                )

        elif self._node_centered():
            if destination == "edge":
                _difference = _calculate_edge_node_difference(
                    self.values, self.uxgrid.edge_node_connectivity.values
                )
                dims[-1] = "n_edge"
                name = f"{var_name}edge_node_difference"
            elif destination == "node":
                raise ValueError(
                    "Invalid destination 'node' for a node-centered data variable, computing"
                    "the difference and storing it on each node is not possible"
                )

            elif destination == "face":
                raise ValueError(
                    "Support for computing the difference of a node-centered data variable and storing"
                    "the result on each face not yet supported."
                )

        elif self._edge_centered():
            raise NotImplementedError(
                "Difference for edge centered data variables not yet implemented"
            )

        else:
            raise ValueError("TODO: ")

        uxda = UxDataArray(
            _difference,
            uxgrid=self.uxgrid,
            name=name,
            dims=dims,
        )

        return uxda

        pass

    def _face_centered(self) -> bool:
        """Returns whether the data stored is Face Centered (i.e. contains the
        "n_face" dimension)"""
        return "n_face" in self.dims

    def _node_centered(self) -> bool:
        """Returns whether the data stored is Node Centered (i.e. contains the
        "n_node" dimension)"""
        return "n_node" in self.dims

    def _edge_centered(self) -> bool:
        """Returns whether the data stored is Edge Centered (i.e. contains the
        "n_edge" dimension)"""
        return "n_edge" in self.dims

    def isel(self, ignore_grid=False, *args, **kwargs):
        """Grid-informed implementation of xarray's ``isel`` method, which
        enables indexing across grid dimensions.

        Subsetting across grid dimensions ('n_node', 'n_edge', or 'n_face') returns will return a new UxDataArray with
        a newly initialized Grid only containing those elements.

        Currently only supports inclusive selection, meaning that for cases where node or edge indices are provided,
        any face that contains that element is included in the resulting subset. This means that additional elements
        beyond those that were initially provided in the indices will be included. Support for more methods, such as
        exclusive and clipped indexing is in the works.

        Parameters
        **kwargs: kwargs
            Dimension to index, one of ['n_node', 'n_edge', 'n_face'] for grid-indexing, or any other dimension for
            regular xarray indexing

        Example
        -------
        > uxda.subset(n_node=[1, 2, 3])
        """

        from uxarray.constants import GRID_DIMS

        if any(grid_dim in kwargs for grid_dim in GRID_DIMS) and not ignore_grid:
            # slicing a grid-dimension through Grid object

            dim_mask = [grid_dim in kwargs for grid_dim in GRID_DIMS]
            dim_count = np.count_nonzero(dim_mask)

            if dim_count > 1:
                raise ValueError("Only one grid dimension can be sliced at a time")

            if "n_node" in kwargs:
                sliced_grid = self.uxgrid.isel(n_node=kwargs["n_node"])
            elif "n_edge" in kwargs:
                sliced_grid = self.uxgrid.isel(n_edge=kwargs["n_edge"])
            else:
                sliced_grid = self.uxgrid.isel(n_face=kwargs["n_face"])

            return self._slice_from_grid(sliced_grid)

        else:
            # original xarray implementation for non-grid dimensions
            return super().isel(*args, **kwargs)

    def _slice_from_grid(self, sliced_grid):
        """Slices a  ``UxDataArray`` from a sliced ``Grid``, using cached
        indices to correctly slice the data variable."""

        from uxarray.core.dataarray import UxDataArray

        if self._face_centered():
            d_var = self.isel(
                n_face=sliced_grid._ds["subgrid_face_indices"], ignore_grid=True
            )

        elif self._edge_centered():
            d_var = self.isel(
                n_edge=sliced_grid._ds["subgrid_edge_indices"], ignore_grid=True
            )

        elif self._node_centered():
            d_var = self.isel(
                n_node=sliced_grid._ds["subgrid_node_indices"], ignore_grid=True
            )

        else:
            raise ValueError(
                "Data variable must be either node, edge, or face centered."
            )

        return UxDataArray(
            uxgrid=sliced_grid,
            data=d_var,
            name=self.name,
            coords=self.coords,
            dims=self.dims,
            attrs=self.attrs,
        )
