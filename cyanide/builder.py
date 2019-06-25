from collections import defaultdict

import numpy as np

from .log import logger
from .mof import MOF
from .scaler import Scaler
from .locator import Locator
from .local_structure import LocalStructure

# bb: building block.
class Builder:
    def build(self, topology, node_bbs, edge_bbs=None, custom_edge_bbs=None):
        """
        The node_bbs must be given with proper order.
        Same as node type order in topology.

        Inputs:
            custom_edge_bbs: Custom edge building blocks at specific edge
                index e. It is a dict, keys are edge index and values are
                building block.
        """
        logger.debug("Builder.build starts.")

        # locator for bb locations.
        locator = Locator()

        if edge_bbs is None:
            edge_bbs = defaultdict(lambda: None)
        else:
            edge_bbs = defaultdict(lambda: None, edge_bbs)

        # make empty dictionary.
        if custom_edge_bbs is None:
            custom_edge_bbs = {}

        assert topology.n_node_types == len(node_bbs)

        # Calculate bonds before start.
        logger.info("Start pre-calculation of bonds in building blocks.")
        for node in node_bbs:
            node.bonds

        for edge in edge_bbs.values():
            if edge is None:
                continue
            edge.bonds

        # Locate nodes and edges.
        located_bbs = [None for _ in range(topology.n_all_points)]
        permutations = [None for _ in range(topology.n_all_points)]
        # Locate nodes.
        for i in topology.node_indices:
            # Get bb.
            t = topology.get_node_type(i)
            node_bb = node_bbs[t]
            # Get target.
            target = topology.local_structure(i)
            # Only orientation.
            # Translations are applied after topology relexation.
            located_node, perm, rmsd = locator.locate(target, node_bb)
            located_bbs[i] = located_node
            # This information used in scaling of topology.
            permutations[i] = perm

            logger.info(f"Pre-location Node {i}, RMSD: {rmsd:.2E}")

        # Just append edges to the buidiling block slots.
        # There is no location in this stage.
        # This information is used in the scaling of topology.
        # All permutations are set to [0, 1] because the edges does not need
        # any permutation estimations for the locations.
        for e in topology.edge_indices:
            if e in custom_edge_bbs:
                edge_bb = custom_edge_bbs[e]
            else:
                ti, tj = topology.get_edge_type(e)
                edge_bb = edge_bbs[(ti, tj)]

            if edge_bb is None:
                continue

            located_bbs[e] = edge_bb
            permutations[e] = np.array([0, 1])

        # Scale topology.
        scaler = Scaler(topology, located_bbs, permutations)
        # Change topology to scaled topology.
        original_topology = topology
        topology = scaler.scale()

        # Relocate and translate node building blocks.
        for i in topology.node_indices:
            perm = permutations[i]
            # Get node bb.
            t = topology.get_node_type(i)
            node_bb = node_bbs[t]
            # Get target.
            target = topology.local_structure(i)
            # Orientation.
            located_node, rmsd = \
                locator.locate_with_permutation(target, node_bb, perm)
            # Translation.
            centroid = topology.atoms.positions[i]
            located_node.set_centroid(centroid)

            # Update.
            located_bbs[i] = located_node

            logger.info(f"Node {i}, RMSD: {rmsd:.3E}")

        # Thie helpers are so verbose. Anoying.
        def find_matched_atom_indices(e):
            """
            Inputs:
                e: Edge index.

            External variables:
                original_topology, located_bbs, permutations.
            """
            topology = original_topology

            # i and j: edge index in topology
            n1, n2 = topology.neighbor_list[e]

            i1 = n1.index
            i2 = n2.index

            bb1 = located_bbs[i1]
            bb2 = located_bbs[i2]

            # Find bonded atom index for i1.
            for o, n in enumerate(topology.neighbor_list[i1]):
                # Check zero sum.
                s = n.distance_vector + n1.distance_vector
                s = np.linalg.norm(s)
                if s < 0.01:
                    perm = permutations[i1]
                    a1 = bb1.connection_point_indices[perm][o]
                    break

            # Find bonded atom index for i2.
            for o, n in enumerate(topology.neighbor_list[i2]):
                # Check zero sum.
                s = n.distance_vector + n2.distance_vector
                s = np.linalg.norm(s)
                if s < 0.01:
                    perm = permutations[i2]
                    a2 = bb2.connection_point_indices[perm][o]
                    break

            return a1, a2

        def calc_image(ni, nj, invc):
            """
            Calculate image number.
            External variables:
                topology.
            """
            # Calculate image.
            # d = d_{ij}
            i = ni.index
            j = nj.index

            d = nj.distance_vector - ni.distance_vector

            ri = topology.atoms.positions[i]
            rj = topology.atoms.positions[j]

            image = (d - (rj-ri)) @ invc

            return image

        # Locate edges.
        logger.info("Start placing edges.")
        c = topology.atoms.cell
        invc = np.linalg.inv(topology.atoms.cell)
        for e in topology.edge_indices:
            edge_bb = located_bbs[e]
            # Neglect no edge cases.
            if edge_bb is None:
                continue

            n1, n2 = topology.neighbor_list[e]

            i1 = n1.index
            i2 = n2.index

            bb1 = located_bbs[i1]
            bb2 = located_bbs[i2]

            a1, a2 = find_matched_atom_indices(e)

            r1 = bb1.atoms.positions[a1]
            r2 = bb2.atoms.positions[a2]

            image = calc_image(n1, n2, invc)
            d = r2 - r1 + image@c

            ## This may outside of the unit cell. Should be changed.
            centroid = r1 + 0.5*d

            if topology.node_types[i1] <= topology.node_types[i2]:
                perm = [0, 1]
            else:
                perm = [1, 0]

            target = LocalStructure(np.array([r1, r1+d]), [i1, i2])
            located_edge, rmsd = \
                locator.locate_with_permutation(target, edge_bb, perm)

            located_edge.set_centroid(centroid)
            located_bbs[e] = located_edge

            logger.info(f"Edge {e}, RMSD: {rmsd:.2E}")

        logger.info("Start finding bonds in generated MOF.")
        logger.info("Start finding bonds in building blocks.")
        # Build bonds of generated MOF.
        index_offsets = [None for _ in range(topology.n_all_points)]
        index_offsets[0] = 0
        for i, bb in enumerate(located_bbs[:-1]):
            if bb is None:
                index_offsets[i+1] = index_offsets[i] + 0
            else:
                index_offsets[i+1] = index_offsets[i] + bb.n_atoms

        bb_bonds = []
        bb_bond_types = []
        for offset, bb in zip(index_offsets, located_bbs):
            if bb is None:
                continue
            bb_bonds.append(bb.bonds + offset)
            bb_bond_types += bb.bond_types
        bb_bonds = np.concatenate(bb_bonds, axis=0)

        logger.info("Start finding bonds between building blocks.")

        # Find bond between building blocks.
        bonds = []
        bond_types = []
        for j in topology.edge_indices:
            a1, a2 = find_matched_atom_indices(j)

            # i and j: edge index in topology
            n1, n2 = topology.neighbor_list[j]
            i1 = n1.index
            i2 = n2.index
            a1 += index_offsets[i1]
            a2 += index_offsets[i2]

            # Edge exists.
            if located_bbs[j] is not None:
                perm = permutations[j]
                e1, e2 = (
                    located_bbs[j].connection_point_indices[perm]
                    + index_offsets[j]
                )
                bonds.append((e1, a1))
                bonds.append((e2, a2))
                bond_types += ["S", "S"]
                logger.info(
                    "Bonds on topology edge %s are connected %s, %s.",
                    j, bonds[-2], bonds[-1],
                )
            else:
                bonds.append((a1, a2))
                bond_types += ["S"]
                logger.info(
                    "Bonds on topology edge %s are connected %s.",
                    j, bonds[-1],
                )

        bonds = np.array(bonds)

        # All bonds in generated MOF.
        all_bonds = np.concatenate([bb_bonds, bonds], axis=0)
        all_bond_types = bb_bond_types + bond_types

        logger.info("Start Making MOF instance.")
        # Make full atoms from located building blocks.
        bb_atoms_list = [v.atoms for v in located_bbs if v is not None]

        logger.debug("Merge list of atoms.")
        mof_atoms = sum(bb_atoms_list[1:], bb_atoms_list[0])
        logger.debug("Set cell and boundary.")
        mof_atoms.set_pbc(True)
        mof_atoms.set_cell(topology.atoms.cell)

        # Remove connection points (X) from the MOF.
        count = 0
        new_indices = {}
        for a in mof_atoms:
            if a.symbol == "X":
                continue
            new_indices[a.index] = count
            count += 1

        def is_X(i):
            return mof_atoms[i].symbol == "X"

        XX_bonds = []
        new_bonds = []
        new_bond_types = []
        X_neighbor_list = defaultdict(list)
        for (i, j), t in zip(all_bonds, all_bond_types):
            if is_X(i) and is_X(j):
                XX_bonds.append((i, j))
            elif is_X(i):
                X_neighbor_list[i] = j
            elif is_X(j):
                X_neighbor_list[j] = i
            else:
                new_bonds.append((i, j))
                new_bond_types.append(t)

        for i, j in XX_bonds:
            new_bonds.append((
                X_neighbor_list[i],
                X_neighbor_list[j]
            ))
            new_bond_types.append("S")

        all_bonds = [
            (new_indices[i], new_indices[j]) for i, j in new_bonds
        ]
        all_bonds = np.array(all_bonds)
        all_bond_types = new_bond_types

        del mof_atoms[[a.symbol == "X" for a in mof_atoms]]

        info = {
            "topology": topology,
            "node_bbs": node_bbs,
            "edge_bbs": edge_bbs,
            "custom_edge_bbs": custom_edge_bbs,
        }

        mof = MOF(mof_atoms, all_bonds, all_bond_types, info=info, wrap=True)
        logger.info("Construction of MOF done.")

        return mof
