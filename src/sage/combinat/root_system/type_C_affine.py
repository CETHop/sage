"""
Root system data for (untwisted) type C affine
"""
#*****************************************************************************
#       Copyright (C) 2008-2009 Nicolas M. Thiery <nthiery at users.sf.net>,
#
#  Distributed under the terms of the GNU General Public License (GPL)
#                  http://www.gnu.org/licenses/
#*****************************************************************************
from cartan_type import CartanType_standard_untwisted_affine
class CartanType(CartanType_standard_untwisted_affine):
    def __init__(self, n):
        """
        EXAMPLES::

            sage: ct = CartanType(['C',4,1])
            sage: ct
            ['C', 4, 1]
            sage: ct._repr_(compact = True)
            'C4~'

            sage: ct.is_irreducible()
            True
            sage: ct.is_finite()
            False
            sage: ct.is_affine()
            True
            sage: ct.is_untwisted_affine()
            True
            sage: ct.is_crystallographic()
            True
            sage: ct.is_simply_laced()
            False
            sage: ct.classical()
            ['C', 4]
            sage: ct.dual()
            ['C', 4, 1]^*
            sage: ct.dual().is_untwisted_affine()
            False

        TESTS::

            sage: TestSuite(ct).run()
        """
        assert n >= 1
        CartanType_standard_untwisted_affine.__init__(self, "C", n)

    def dynkin_diagram(self):
        """
        Returns the extended Dynkin diagram for affine type C.

        EXAMPLES::

            sage: c = CartanType(['C',3,1]).dynkin_diagram()
            sage: c
             O=>=O---O=<=O
             0   1   2   3
             C3~
            sage: sorted(c.edges())
            [(0, 1, 2), (1, 0, 1), (1, 2, 1), (2, 1, 1), (2, 3, 1), (3, 2, 2)]

        """
        n = self.n
        if n == 1:
            import cartan_type
            res = cartan_type.CartanType(["A",1,1]).dynkin_diagram()
            res._cartan_type = self
            return res
        from dynkin_diagram import DynkinDiagram_class
        g = DynkinDiagram_class(self)
        for i in range(1, n):
            g.add_edge(i, i+1)
        g.set_edge_label(n,n-1,2)
        g.add_edge(0,1,2)
        return g

    def _latex_dynkin_diagram(self, label=lambda x: x, node_dist=2, dual=False):
        r"""
        Return a latex representation of the Dynkin diagram.

        EXAMPLES::

            sage: print CartanType(['C',4,1])._latex_dynkin_diagram()
            \draw (0, 0.1 cm) -- +(2 cm,0);
            \draw (0, -0.1 cm) -- +(2 cm,0);
            \draw[shift={(1.2, 0)}, rotate=0] (135 : 0.45cm) -- (0,0) -- (-135 : 0.45cm);
            {
            \pgftransformxshift{2 cm}
            \draw (0 cm,0) -- (4 cm,0);
            \draw (4 cm, 0.1 cm) -- +(2 cm,0);
            \draw (4 cm, -0.1 cm) -- +(2 cm,0);
            \draw[shift={(4.8, 0)}, rotate=180] (135 : 0.45cm) -- (0,0) -- (-135 : 0.45cm);
            \draw[fill=white] (0 cm, 0) circle (.25cm) node[below=4pt]{$1$};
            \draw[fill=white] (2 cm, 0) circle (.25cm) node[below=4pt]{$2$};
            \draw[fill=white] (4 cm, 0) circle (.25cm) node[below=4pt]{$3$};
            \draw[fill=white] (6 cm, 0) circle (.25cm) node[below=4pt]{$4$};
            }
            \draw[fill=white] (0, 0) circle (.25cm) node[below=4pt]{$0$};
            sage: print CartanType(['C',4,1]).dual()._latex_dynkin_diagram()
            \draw (0, 0.1 cm) -- +(2 cm,0);
            \draw (0, -0.1 cm) -- +(2 cm,0);
            \draw[shift={(0.8, 0)}, rotate=180] (135 : 0.45cm) -- (0,0) -- (-135 : 0.45cm);
            {
            \pgftransformxshift{2 cm}
            \draw (0 cm,0) -- (4 cm,0);
            \draw (4 cm, 0.1 cm) -- +(2 cm,0);
            \draw (4 cm, -0.1 cm) -- +(2 cm,0);
            \draw[shift={(5.2, 0)}, rotate=0] (135 : 0.45cm) -- (0,0) -- (-135 : 0.45cm);
            \draw[fill=white] (0 cm, 0) circle (.25cm) node[below=4pt]{$1$};
            \draw[fill=white] (2 cm, 0) circle (.25cm) node[below=4pt]{$2$};
            \draw[fill=white] (4 cm, 0) circle (.25cm) node[below=4pt]{$3$};
            \draw[fill=white] (6 cm, 0) circle (.25cm) node[below=4pt]{$4$};
            }
            \draw[fill=white] (0, 0) circle (.25cm) node[below=4pt]{$0$};
        """
        if self.n == 1:
            import cartan_type
            return cartan_type.CartanType(["A",1,1])._latex_dynkin_diagram(label, node_dist)
        if self.global_options('mark_special_node') in ['latex', 'both']:
            special_fill = 'black'
        else:
            special_fill = 'white'
        ret = "\\draw (0, 0.1 cm) -- +(%s cm,0);\n"%node_dist
        ret += "\\draw (0, -0.1 cm) -- +(%s cm,0);\n"%node_dist
        if dual:
            ret += self._latex_draw_arrow_tip(0.5*node_dist-0.2, 0, 180)
        else:
            ret += self._latex_draw_arrow_tip(0.5*node_dist+0.2, 0, 0)
        ret += "{\n\\pgftransformxshift{%s cm}\n"%node_dist
        ret += self.classical()._latex_dynkin_diagram(label, node_dist, dual)
        ret += "\n}\n\\draw[fill=%s] (0, 0) circle (.25cm) node[below=4pt]{$%s$};"%(special_fill, label(0))
        return ret

    def ascii_art(self, label = lambda x: x):
        """
        Returns a ascii art representation of the extended Dynkin diagram

        EXAMPLES::

            sage: print CartanType(['C',5,1]).ascii_art(label = lambda x: x+2)
            O=>=O---O---O---O=<=O
            2   3   4   5   6   7

            sage: print CartanType(['C',3,1]).ascii_art()
            O=>=O---O=<=O
            0   1   2   3

            sage: print CartanType(['C',2,1]).ascii_art()
            O=>=O=<=O
            0   1   2

            sage: print CartanType(['C',1,1]).ascii_art()
            O<=>O
            0   1
        """
        n = self.n
        from cartan_type import CartanType
        if n == 1:
            return CartanType(["A",1,1]).ascii_art(label)
        if self.global_options('mark_special_node') in ['printing', 'both']:
            special_str = self.global_options('special_node_str')
        else:
            special_str = 'O'
        ret = "%s=>=O"%special_str + (n-2)*"---O"+"=<=O\n%s   "%label(0)
        ret += "   ".join("%s"%label(i) for i in range(1,n+1))
        return ret

