"""
Trocar cores de PDFs vetoriais (CAD) - interface PySide6 / Qt Widgets.

Dependências:
    pip install PySide6 pikepdf

Uso:
    python trocar_cores_gui.py
"""
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pikepdf
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QColorDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# --------------------------------------------------------------------------
# Modelos
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Cor:
    nome: str
    r: int
    g: int
    b: int

    @property
    def hex(self) -> str:
        return f"#{self.r:02x}{self.g:02x}{self.b:02x}"

    @property
    def rgb_float(self) -> tuple[float, float, float]:
        return (self.r / 255, self.g / 255, self.b / 255)

    @property
    def rgb(self) -> tuple[int, int, int]:
        return (self.r, self.g, self.b)

    @classmethod
    def de_qcolor(cls, qcolor: QColor) -> "Cor":
        c = cls("", qcolor.red(), qcolor.green(), qcolor.blue())
        return cls(c.hex.upper(), c.r, c.g, c.b)


# 8 cores padrão do AutoCAD (ACI 1 a 6 + branco e preto, que é o ACI 7)
CORES_AUTOCAD = [
    Cor("Vermelho", 255, 0, 0),
    Cor("Amarelo", 255, 255, 0),
    Cor("Verde", 0, 255, 0),
    Cor("Ciano", 0, 255, 255),
    Cor("Azul", 0, 0, 255),
    Cor("Magenta", 255, 0, 255),
    Cor("Branco", 255, 255, 255),
    Cor("Preto", 0, 0, 0),
]


@dataclass
class Config:
    alvos: list[Cor]
    nova: Cor
    tolerancia: float  # 0 a 1, diferença máxima por canal
    sufixo: str
    pasta_saida: Path | None


@dataclass
class ResultadoArquivo:
    entrada: Path
    saida: Path | None = None
    trocas: int = 0
    erro: str = ""


@dataclass
class ResultadoStream:
    novo: bytes | None = None
    trocas: int = 0


@dataclass
class Contador:
    trocas: int = 0


# --------------------------------------------------------------------------
# Lógica de PDF
# --------------------------------------------------------------------------

OPS_GRAY = {"g", "G"}
OPS_RGB = {"rg", "RG"}
OPS_CMYK = {"k", "K"}
OPS_GENERICOS = {"sc", "scn", "SC", "SCN"}
OPS_COR = OPS_GRAY | OPS_RGB | OPS_CMYK | OPS_GENERICOS


def _rgb_do_operador(operador: str, operandos: list) -> tuple[float, float, float] | None:
    """Converte os operandos de um operador de cor para RGB (0 a 1)."""
    try:
        valores = [float(x) for x in operandos]
    except (TypeError, ValueError):  # ex.: nome de pattern em scn
        return None
    n = len(valores)
    if operador in OPS_GRAY and n == 1:
        return (valores[0], valores[0], valores[0])
    if (operador in OPS_RGB or operador in OPS_GENERICOS) and n == 3:
        return (valores[0], valores[1], valores[2])
    if (operador in OPS_CMYK or operador in OPS_GENERICOS) and n == 4:
        c, m, y, k = valores
        return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    return None


def _casa_com_alvo(rgb: tuple[float, float, float], config: Config) -> bool:
    limite = config.tolerancia + 0.005
    for alvo in config.alvos:
        if max(abs(a - b) for a, b in zip(rgb, alvo.rgb_float)) <= limite:
            return True
    return False


def _rgb_para_cmyk(r: float, g: float, b: float) -> list[float]:
    k = 1 - max(r, g, b)
    if k >= 1:
        return [0.0, 0.0, 0.0, 1.0]
    return [(1 - r - k) / (1 - k), (1 - g - k) / (1 - k), (1 - b - k) / (1 - k), k]


def _substituicao(operador: str, n_operandos: int, nova: Cor) -> tuple[str, list[float]]:
    """Mantém o espaço de cor original quando possível."""
    r, g, b = nova.rgb_float
    if operador in OPS_CMYK or (operador in OPS_GENERICOS and n_operandos == 4):
        return operador, _rgb_para_cmyk(r, g, b)
    if operador in OPS_GRAY:  # cinza não representa cor: vira RGB
        return ("rg" if operador == "g" else "RG"), [r, g, b]
    return operador, [r, g, b]


def _processar_stream(objeto, config: Config) -> ResultadoStream:
    novas = []
    trocas = 0
    for instr in pikepdf.parse_content_stream(objeto):
        if isinstance(instr, pikepdf.ContentStreamInlineImage):
            novas.append(instr)
            continue
        operador = str(instr.operator)
        operandos = list(instr.operands)
        if operador in OPS_COR:
            rgb = _rgb_do_operador(operador, operandos)
            if rgb is not None and _casa_com_alvo(rgb, config):
                op_novo, ops_novos = _substituicao(operador, len(operandos), config.nova)
                novas.append((ops_novos, pikepdf.Operator(op_novo)))
                trocas += 1
                continue
        novas.append(instr)
    if trocas == 0:
        return ResultadoStream()
    return ResultadoStream(novo=pikepdf.unparse_content_stream(novas), trocas=trocas)


def _percorrer_xobjects(recursos, config: Config, contador: Contador, visitados: set) -> None:
    if recursos is None or "/XObject" not in recursos:
        return
    for _, xobj in recursos.XObject.items():
        if xobj.objgen in visitados or xobj.get("/Subtype") != "/Form":
            continue
        visitados.add(xobj.objgen)
        res = _processar_stream(xobj, config)
        if res.novo is not None:
            xobj.write(res.novo)
            contador.trocas += res.trocas
        _percorrer_xobjects(xobj.get("/Resources"), config, contador, visitados)


def processar_pdf(entrada: Path, saida: Path, config: Config) -> ResultadoArquivo:
    contador = Contador()
    visitados: set = set()
    try:
        with pikepdf.open(entrada) as pdf:
            for pagina in pdf.pages:
                res = _processar_stream(pagina, config)
                if res.novo is not None:
                    pagina.obj.Contents = pdf.make_stream(res.novo)
                    contador.trocas += res.trocas
                _percorrer_xobjects(pagina.obj.get("/Resources"), config, contador, visitados)
            pdf.save(saida)
    except Exception as exc:  # noqa: BLE001 - mostramos o erro na interface
        return ResultadoArquivo(entrada=entrada, erro=str(exc))
    return ResultadoArquivo(entrada=entrada, saida=saida, trocas=contador.trocas)


def caminho_saida(entrada: Path, config: Config) -> Path:
    pasta = config.pasta_saida or entrada.parent
    return pasta / f"{entrada.stem}{config.sufixo}.pdf"


class Trabalho(QThread):
    progresso = Signal(int, int)
    arquivo_pronto = Signal(object)  # ResultadoArquivo

    def __init__(self, arquivos: list[Path], config: Config):
        super().__init__()
        self._arquivos = arquivos
        self._config = config

    def run(self) -> None:
        total = len(self._arquivos)
        for i, arq in enumerate(self._arquivos, start=1):
            resultado = processar_pdf(arq, caminho_saida(arq, self._config), self._config)
            self.arquivo_pronto.emit(resultado)
            self.progresso.emit(i, total)


# --------------------------------------------------------------------------
# Widgets
# --------------------------------------------------------------------------


class Amostra(QToolButton):
    """Botão quadrado com a cor; marcado = selecionado."""

    def __init__(self, cor: Cor, personalizada: bool = False):
        super().__init__()
        self.cor = cor
        self.personalizada = personalizada
        self.setCheckable(True)
        self.setFixedSize(84, 56)
        self.setToolTip(f"{cor.nome}  {cor.hex}")
        luminancia = 0.299 * cor.r + 0.587 * cor.g + 0.114 * cor.b
        texto = "#000000" if luminancia > 140 else "#ffffff"
        self.setStyleSheet(
            f"QToolButton {{ background: {cor.hex}; color: {texto};"
            f" border: 2px solid #777; border-radius: 6px; font-weight: bold; }}"
            f"QToolButton:checked {{ border: 4px solid #ff9800; }}"
        )
        self.toggled.connect(self._atualizar_texto)
        self._atualizar_texto(False)

    def _atualizar_texto(self, marcado: bool) -> None:
        self.setText(("✓ " if marcado else "") + self.cor.nome)


class SeletorCores(QGroupBox):
    COLUNAS = 4

    def __init__(self, titulo: str, multiplo: bool, padrao: list[str]):
        super().__init__(titulo)
        self._multiplo = multiplo
        self._amostras: list[Amostra] = []
        self._grupo = QButtonGroup(self)
        self._grupo.setExclusive(not multiplo)

        self._grade = QGridLayout()
        self._grade.setSpacing(8)
        for cor in CORES_AUTOCAD:
            amostra = self._criar(cor, personalizada=False)
            amostra.setChecked(cor.nome in padrao)

        botao = QPushButton("Cor personalizada…")
        botao.clicked.connect(self._escolher_personalizada)
        self._info = QLabel("")
        self._info.setStyleSheet("color: gray;")

        layout = QVBoxLayout(self)
        layout.addLayout(self._grade)
        layout.addStretch(1)
        layout.addWidget(botao)
        layout.addWidget(self._info)

    def _criar(self, cor: Cor, personalizada: bool) -> Amostra:
        amostra = Amostra(cor, personalizada)
        i = len(self._amostras)
        self._amostras.append(amostra)
        self._grupo.addButton(amostra)
        self._grade.addWidget(amostra, i // self.COLUNAS, i % self.COLUNAS)
        return amostra

    def _remover_personalizadas(self) -> None:
        for amostra in [a for a in self._amostras if a.personalizada]:
            self._amostras.remove(amostra)
            self._grupo.removeButton(amostra)
            self._grade.removeWidget(amostra)
            amostra.deleteLater()

    def _escolher_personalizada(self) -> None:
        qcor = QColorDialog.getColor(QColor(255, 255, 255), self, "Escolher cor")
        if not qcor.isValid():
            return
        cor = Cor.de_qcolor(qcor)
        for amostra in self._amostras:  # já existe? só marca
            if amostra.cor.rgb == cor.rgb:
                amostra.setChecked(True)
                return
        if not self._multiplo:  # cor nova: só uma personalizada por vez
            self._remover_personalizadas()
        self._criar(cor, personalizada=True).setChecked(True)

    def cores_selecionadas(self) -> list[Cor]:
        return [a.cor for a in self._amostras if a.isChecked()]


# --------------------------------------------------------------------------
# Janela principal
# --------------------------------------------------------------------------


class Janela(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Trocar cores de PDF")
        self.setAcceptDrops(True)
        self._trabalho: Trabalho | None = None

        central = QWidget()
        self.setCentralWidget(central)
        raiz = QVBoxLayout(central)

        # Cabeçalho
        titulo = QLabel("Trocar cores de linhas em PDFs vetoriais")
        titulo.setStyleSheet("font-size: 20px; font-weight: bold;")
        sub = QLabel("Escolha as cores a trocar, a cor nova e carregue os PDFs.")
        sub.setStyleSheet("color: gray;")
        raiz.addWidget(titulo)
        raiz.addWidget(sub)

        # Duas colunas
        self.alvos = SeletorCores("Cor alvo (pode marcar várias)", True, ["Vermelho", "Magenta"])
        self.nova = SeletorCores("Cor nova", False, ["Azul"])
        colunas = QHBoxLayout()
        colunas.addWidget(self.alvos, 1)
        colunas.addWidget(self.nova, 1)
        raiz.addLayout(colunas)

        # Arquivos
        grupo_arq = QGroupBox("Arquivos (também dá para arrastar e soltar)")
        lay_arq = QVBoxLayout(grupo_arq)
        self.lista = QListWidget()
        self.lista.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        lay_arq.addWidget(self.lista)
        botoes = QHBoxLayout()
        for texto, slot in [
            ("Adicionar arquivos…", self._adicionar_arquivos),
            ("Adicionar pasta…", self._adicionar_pasta),
            ("Remover selecionados", self._remover_selecionados),
            ("Limpar lista", self.lista.clear),
        ]:
            b = QPushButton(texto)
            b.clicked.connect(slot)
            botoes.addWidget(b)
        lay_arq.addLayout(botoes)
        raiz.addWidget(grupo_arq, 1)

        # Opções
        opcoes = QHBoxLayout()
        opcoes.addWidget(QLabel("Tolerância:"))
        self.tolerancia = QSpinBox()
        self.tolerancia.setRange(0, 50)
        self.tolerancia.setValue(10)
        self.tolerancia.setSuffix(" %")
        self.tolerancia.setToolTip("Quanto a cor do PDF pode diferir da cor alvo e ainda ser trocada")
        opcoes.addWidget(self.tolerancia)
        opcoes.addWidget(QLabel("Sufixo:"))
        self.sufixo = QLineEdit("_azul")
        self.sufixo.setMaximumWidth(120)
        opcoes.addWidget(self.sufixo)
        opcoes.addWidget(QLabel("Saída:"))
        self.pasta_saida = QLineEdit()
        self.pasta_saida.setPlaceholderText("Mesma pasta do original")
        opcoes.addWidget(self.pasta_saida, 1)
        b_pasta = QPushButton("Escolher…")
        b_pasta.clicked.connect(self._escolher_pasta_saida)
        opcoes.addWidget(b_pasta)
        raiz.addLayout(opcoes)

        # Converter + progresso + log
        self.btn_converter = QPushButton("Converter")
        self.btn_converter.setStyleSheet("font-weight: bold; padding: 8px;")
        self.btn_converter.clicked.connect(self._converter)
        self.barra = QProgressBar()
        self.barra.setVisible(False)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(130)
        raiz.addWidget(self.btn_converter)
        raiz.addWidget(self.barra)
        raiz.addWidget(self.log)

    # ---- arquivos ---------------------------------------------------------

    def _adicionar_caminhos(self, caminhos: list[Path]) -> None:
        existentes = {self.lista.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.lista.count())}
        sufixo = self.sufixo.text()
        for caminho in caminhos:
            if caminho.is_dir():
                pdfs = sorted(p for p in caminho.iterdir() if p.suffix.lower() == ".pdf")
                pdfs = [p for p in pdfs if not (sufixo and p.stem.endswith(sufixo))]
                self._adicionar_caminhos(pdfs)
                continue
            if caminho.suffix.lower() != ".pdf" or str(caminho) in existentes:
                continue
            item = QListWidgetItem(str(caminho))
            item.setData(Qt.ItemDataRole.UserRole, str(caminho))
            self.lista.addItem(item)
            existentes.add(str(caminho))

    def _adicionar_arquivos(self) -> None:
        nomes, _ = QFileDialog.getOpenFileNames(self, "Selecionar PDFs", "", "PDF (*.pdf)")
        self._adicionar_caminhos([Path(n) for n in nomes])

    def _adicionar_pasta(self) -> None:
        pasta = QFileDialog.getExistingDirectory(self, "Selecionar pasta")
        if pasta:
            self._adicionar_caminhos([Path(pasta)])

    def _remover_selecionados(self) -> None:
        for item in self.lista.selectedItems():
            self.lista.takeItem(self.lista.row(item))

    def _escolher_pasta_saida(self) -> None:
        pasta = QFileDialog.getExistingDirectory(self, "Pasta de saída")
        if pasta:
            self.pasta_saida.setText(pasta)

    def dragEnterEvent(self, evento) -> None:
        if evento.mimeData().hasUrls():
            evento.acceptProposedAction()

    def dropEvent(self, evento) -> None:
        self._adicionar_caminhos([Path(u.toLocalFile()) for u in evento.mimeData().urls()])

    # ---- conversão --------------------------------------------------------

    def _montar_config(self) -> Config | None:
        alvos = self.alvos.cores_selecionadas()
        novas = self.nova.cores_selecionadas()
        if not alvos or not novas:
            QMessageBox.warning(self, "Cores", "Marque ao menos uma cor alvo e uma cor nova.")
            return None
        sufixo = self.sufixo.text().strip()
        pasta_txt = self.pasta_saida.text().strip()
        if not sufixo and not pasta_txt:
            QMessageBox.warning(
                self, "Saída", "Sem sufixo e sem pasta de saída o original seria sobrescrito."
            )
            return None
        pasta = Path(pasta_txt) if pasta_txt else None
        if pasta is not None:
            pasta.mkdir(parents=True, exist_ok=True)
        return Config(
            alvos=alvos,
            nova=novas[0],
            tolerancia=self.tolerancia.value() / 100,
            sufixo=sufixo,
            pasta_saida=pasta,
        )

    def _converter(self) -> None:
        arquivos = [
            Path(self.lista.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.lista.count())
        ]
        if not arquivos:
            QMessageBox.information(self, "Arquivos", "Adicione ao menos um PDF.")
            return
        config = self._montar_config()
        if config is None:
            return

        self.log.clear()
        self.btn_converter.setEnabled(False)
        self.barra.setRange(0, len(arquivos))
        self.barra.setValue(0)
        self.barra.setVisible(True)

        self._trabalho = Trabalho(arquivos, config)
        self._trabalho.arquivo_pronto.connect(self._arquivo_pronto)
        self._trabalho.progresso.connect(lambda feito, _total: self.barra.setValue(feito))
        self._trabalho.finished.connect(self._finalizado)
        self._trabalho.start()

    def _arquivo_pronto(self, r: ResultadoArquivo) -> None:
        if r.erro:
            self.log.appendPlainText(f"✗ {r.entrada.name}: {r.erro}")
        elif r.trocas == 0:
            self.log.appendPlainText(f"⚠ {r.entrada.name}: nenhuma cor encontrada (salvo sem alterações)")
        else:
            self.log.appendPlainText(f"✓ {r.entrada.name} → {r.saida.name} ({r.trocas} trocas)")

    def _finalizado(self) -> None:
        self.btn_converter.setEnabled(True)
        self.log.appendPlainText("Concluído.")


def main() -> None:
    app = QApplication(sys.argv)
    janela = Janela()
    janela.resize(860, 820)
    janela.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
